"""Фоновая склейка: копирование, NVENC и резервный CPU с прогрессом."""

import queue
import os
import math
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from fractions import Fraction
from debug_log import journal, log_ffmpeg_version
from black_border import detect_border

from ffmpeg import (NO_WINDOW, MediaInfo, detect_field_order, nvenc_available,
                    nvenc_rate_hint_supported, probe_media, progress_seconds)
from settings import AUDIO_QUALITIES, FRAME_RATES, RESOLUTIONS

# Число соседних кадров, учитываемых при сглаживании движения камеры.
# Большие значения дают более плавную картинку, но сильнее ограничивают
# намеренное движение камеры и могут потребовать большей обрезки краёв.
STABILIZATION_SMOOTHING = {"Слабый": 5, "Средний": 20, "Сильный": 60}
QUALITY_BITRATE = {"Максимальное": 0.90, "Высокое": 0.70,
                   "Компактное": 0.45, "Малое": 0.25}
# Ограничиваем бюджет H.264 по числу выходных пикселей в секунду:
# высокий битрейт старого DV не должен раздувать результат SD-разрешения.
QUALITY_PIXEL_BUDGET = {"Максимальное": 0.30, "Высокое": 0.20,
                        "Компактное": 0.14, "Малое": 0.10}
# У OpenCL другой алгоритм и другая шкала сглаживания, чем у vidstab.
OPENCL_SMOOTHING = {"Слабый": 0.2, "Средний": 0.5, "Сильный": 0.8}
OPENCL_WINDOW = {"Слабый": 0.5, "Средний": 1.0, "Сильный": 2.0}
# По умолчанию FFmpeg выделяет число потоков фильтра по всем логическим
# ядрам. Для десятков входов это создаёт сотни потоков и расходует гигабайты
# памяти; небольшому графу достаточно ограниченного пула.
FILTER_THREADS = min(4, os.cpu_count() or 4)


def progress_position(seconds: float, durations: list[float]) -> tuple[int, int, int]:
    """Определить ролик и два процента по позиции в итоговой дорожке."""
    total = sum(durations)
    elapsed = max(0.0, seconds)
    for index, duration in enumerate(durations):
        if elapsed < duration or index == len(durations) - 1:
            local = min(99, max(0, int(elapsed / duration * 100))) if duration > 0 else 0
            overall = min(99, max(0, int(seconds / total * 100))) if total > 0 else 0
            return index + 1, local, overall
        elapsed -= duration
    return 1, 0, 0


def concat_entry(path: Path) -> str:
    """Экранировать путь по правилам списка файлов concat demuxer FFmpeg."""
    return "file '" + path.resolve().as_posix().replace("'", "'\\''") + "'\n"


def encoding_attempts(ffmpeg: str, compress: bool, use_gpu: bool = True,
                       stopping: threading.Event | None = None) -> Iterator[str]:
    """Без сжатия только копировать, при сжатии выбрать NVENC или CPU.

    При копировании видеокарта даже не проверяется; ошибок копирования
    нельзя исправлять незаметным перекодированием исходных потоков.
    """
    if not compress:
        yield "copy"
        return
    if use_gpu and nvenc_available(ffmpeg, stopping=stopping):
        yield "nvenc"
    yield "cpu"


def video_filter(order: str | None) -> str:
    """Собрать одинаковую подготовку кадров для обоих проходов стабилизации."""
    deinterlace = (f"bwdif=mode=send_field:parity={order}:deint=all"
                   if order else "null")
    return f"setpts=PTS-STARTPTS,{deinterlace},format=yuv420p,setsar=1"


def auto_frame_rate(source_rate: str | None) -> str | None:
    """Сократить высокую частоту вдвое, сохранив дробный телевизионный шаг."""
    if source_rate is None:
        return None
    try:
        rate = float(source_rate)
    except ValueError:
        return None
    if 58 <= rate <= 61:
        return "30000/1001" if abs(rate - 59.94) < 0.04 else "30"
    if 49 <= rate <= 51:
        return "25"
    return None


def rate_control_frame_rate(metadata: list[MediaInfo], field_orders: list[str | None],
                            auto_rates: list[str | None] | None) -> str | None:
    """Подсказать кодировщику среднее число кадров в секунду, не меняя PTS.

    concat может терять объявленный FPS. Без подсказки NVENC рассчитывает
    биты на кадр для 30 FPS и почти удваивает размер реального 60 FPS.
    x264 без подсказки принимает микросекундную шкалу concat за миллион
    FPS, ошибается в уровне H.264 и резко ухудшает первые кадры при VBR.
    """
    frames = 0.0
    duration = 0.0
    for index, info in enumerate(metadata):
        rate = auto_rates[index] if auto_rates and auto_rates[index] else info.frame_rate
        try:
            value = float(Fraction(rate))
        except (ValueError, TypeError, ZeroDivisionError):
            return None
        if not math.isfinite(value) or value <= 0 or not info.duration or info.duration <= 0:
            return None
        if field_orders[index] and not (auto_rates and auto_rates[index]):
            value *= 2
        frames += value * info.duration
        duration += info.duration
    return str(Fraction(frames / duration).limit_denominator(100000)) if duration else None


def video_bitrate_budget(ffmpeg: str, videos: list[Path], quality: str,
                         metadata: list[MediaInfo] | None = None, *,
                         field_orders: list[str | None] | None = None,
                          auto_rates: list[str | None] | None = None,
                          output_size: tuple[int, int] | None = None) -> tuple[int, int]:
    """Усреднить бюджеты роликов по времени, учитывая размеры и выходной FPS."""
    if quality not in QUALITY_BITRATE:
        raise RuntimeError("Недопустимое качество видео")
    total_bits = 0.0
    total_seconds = 0.0
    target_bits = 0.0
    infos = metadata if metadata is not None else [probe_media(ffmpeg, video) for video in videos]
    for index, (video, info) in enumerate(zip(videos, infos, strict=True)):
        full_duration = info.duration
        if full_duration is None or full_duration <= 0:
            raise RuntimeError(f"Не удалось определить длительность видео: {video.name}")
        source = info.estimated_video_bitrate(video, full_duration)
        target = source * QUALITY_BITRATE[quality]
        rate = auto_rates[index] if auto_rates and auto_rates[index] else info.frame_rate
        try:
            fps = float(Fraction(rate)) if rate else 0.0
        except ValueError:
            fps = 0.0
        if not (auto_rates and auto_rates[index]) and field_orders and field_orders[index]:
            fps *= 2  # bwdif send_field создаёт кадр из каждого поля.
        # При неполных метаданных остаётся процент исходного битрейта.
        width, height = output_size or (info.width, info.height)
        if width and height and fps > 0 and math.isfinite(fps):
            target = min(target, width * height * fps * QUALITY_PIXEL_BUDGET[quality])
        total_bits += source * full_duration
        target_bits += target * full_duration
        total_seconds += full_duration
    if total_seconds <= 0:
        raise RuntimeError("Нет видео для перекодирования")
    source_bitrate = int(total_bits / total_seconds)
    target_bitrate = max(1, int(target_bits / total_seconds))
    return target_bitrate, max(target_bitrate, min(source_bitrate, int(target_bitrate * 1.5)))


def output_dimensions(metadata: list[MediaInfo], resolution: str) -> tuple[int, int] | None:
    """Выбрать общий размер кадра; H.264 yuv420p требует чётные стороны."""
    if resolution == "1080p":
        return 1920, 1080
    if resolution == "720p":
        return 1280, 720
    if metadata and metadata[0].width and metadata[0].height:
        return max(2, metadata[0].width // 2 * 2), max(2, metadata[0].height // 2 * 2)
    return None


def audio_needs_conversion(metadata: list[MediaInfo]) -> bool:
    """Разные кодеки, параметры или отсутствие звука требуют единой дорожки."""
    return len({(info.has_audio, info.audio_signature) for info in metadata}) > 1


def estimate_output_size(videos: list[Path], metadata: list[MediaInfo], compress: bool,
                         quality: str, frame_rate: str, audio_quality: str, resolution: str,
                         field_orders: list[str | None] | None = None) -> int:
    """Оценить размер из уже прочитанных метаданных, не запуская FFmpeg."""
    if not compress:
        return sum(info.file_size for info in metadata)
    rates = [auto_frame_rate(info.frame_rate) for info in metadata] if frame_rate == "Авто" else None
    bitrate, _ = video_bitrate_budget("", videos, quality, metadata, field_orders=field_orders,
                                     auto_rates=rates, output_size=output_dimensions(metadata, resolution))
    audio = 0.0
    convert = audio_needs_conversion(metadata)
    for info in metadata:
        if audio_quality != "Оригинал" or convert:
            rate = int(audio_quality.split()[1]) * 1000 if audio_quality != "Оригинал" else 192000
            rate = rate if any(item.has_audio for item in metadata) else 0
        else:
            rate = (info.audio_bitrate or 192000) if info.has_audio else 0
        audio += rate * (info.duration or 0)
    return int((bitrate * sum(info.duration or 0 for info in metadata) + audio) / 8 * 1.01)


def border_filter(crop: tuple[int, int, int, int] | None, info: MediaInfo) -> str:
    """Обрезать найденную кромку и восстановить кадр без растяжения пропорций."""
    if crop is None:
        return ""
    width, height, x, y = crop
    return (f",crop={width}:{height}:{x}:{y}:exact=1,"
            f"scale={info.width}:{info.height}:force_original_aspect_ratio=increase:force_divisible_by=2,"
            f"crop={info.width}:{info.height},setsar=1")


def filtered_inputs(manifest: Path, videos: list[Path], field_orders: list[str | None],
                      stabilization: str = "Выключен", gpu_deinterlace: bool = False,
                      gpu_stabilization: bool = False, frame_rate: str | None = None,
                       auto_rates: list[str | None] | None = None, *,
                       output_size: tuple[int, int] | None = None,
                       metadata: list[MediaInfo] | None = None,
                        audio_infos: list[MediaInfo] | None = None,
                        border_crops: list[tuple[int, int, int, int] | None] | None = None,
                        remove_reflection: bool = False) -> list[str]:
    """Собрать видеодорожку по частям, не размывая прогрессивные фрагменты.

    Звук по-прежнему читается через исходный concat demuxer. Видео каждого
    входа обрабатывается отдельно: bwdif получает определённый порядок полей,
    а прогрессивное видео проходит без деинтерлейсинга. При выборе NVENC
    bwdif_cuda обрабатывает поля в памяти GPU. Для OpenCL-стабилизации кадры
    передаются с CUDA на устройство NVIDIA через системную память; concat
    получает обычные кадры. CPU-стабилизация использует подготовку кадров,
    совпадающую с проходом анализа движений.
    """
    graph: list[str] = []
    input_offset = 0 if audio_infos is not None else 1
    stabilizing = stabilization in STABILIZATION_SMOOTHING
    for index, order in enumerate(field_orders):
        if gpu_deinterlace and order:
            filters = ("format=yuv420p,setsar=1,hwupload_cuda" if gpu_stabilization
                       else "setpts=PTS-STARTPTS,format=yuv420p,setsar=1,hwupload_cuda")
            filters += (f",bwdif_cuda=mode=send_field:parity={order}:deint=all"
                        ",hwdownload,format=yuv420p")
        else:
            filters = ("format=yuv420p,setsar=1" if gpu_stabilization else video_filter(order))
        if border_crops and metadata:
            filters += border_filter(border_crops[index], metadata[index])
        if gpu_stabilization and stabilizing:
            # Автообрезка OpenCL накапливает сужение области на длинных
            # роликах и может превратить кадр в чёрный. Оставляем отражение краёв.
            filters += (",hwupload,deshake_opencl="
                        f"smooth_strength={OPENCL_SMOOTHING[stabilization]}:"
                        f"smooth_window_multiplier={OPENCL_WINDOW[stabilization]}:adaptive_crop=0,"
                        "hwdownload,format=yuv420p,setpts=PTS-STARTPTS")
        elif stabilizing:
            # Ограничиваем коррекцию, чтобы движение объектов и неуверенные
            # оценки не заставляли весь кадр плыть. Чёрная кромка не содержит
            # фрагментов предыдущего кадра; статический zoom скрывает её.
            info = metadata[index] if metadata else None
            max_shift = max(1, min(500, int(min(info.width, info.height) * 0.02))) if (
                info and info.width and info.height) else 10
            filters += (f",vidstabtransform=input=stab-{index}.trf:"
                         f"smoothing={STABILIZATION_SMOOTHING[stabilization]}:"
                         f"maxshift={max_shift}:maxangle=0.01:optalgo=gauss:crop=black:"
                         "interpol=bicubic:"
                         + ("optzoom=0:zoom=0" if remove_reflection else "optzoom=1"))
        if auto_rates and auto_rates[index]:
            filters += f",fps={auto_rates[index]}"
        if output_size:
            width, height = output_size
            sar = metadata[index].sample_aspect_ratio if metadata else 1.0
            # Учесть несquare-пиксели DV до вписывания в общий кадр.
            filters += (f",setsar={sar},scale=w='trunc(iw*sar/2)*2':h=ih,setsar=1,"
                        f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1")
        graph.append(f"[{index + input_offset}:v:0]{filters}[v{index}]")
    sources = "".join(f"[v{index}]" for index in range(len(videos)))
    # OpenCL теряет объявленную частоту на выходе: NVENC назначает кадрам
    # длительность по умолчанию (например, 33 мс вместо 16 мс при 60 fps).
    # Для серии с одинаковым fps восстановим частоту графа после concat.
    # Авто применяется к каждому ролику до concat, не повышая частоту остальных.
    graph.append(f"{sources}concat=n={len(videos)}:v=1:a=0"
                  f"{f',fps={frame_rate}' if frame_rate else ''}[v]")
    audio_map = ["-map", "0:a:0?"]
    if audio_infos is not None:
        audio_map = []
        if any(info.has_audio for info in audio_infos):
            for index, info in enumerate(audio_infos):
                source = (f"[{index}:a:0]aresample=48000:async=1:first_pts=0,"
                          "aformat=sample_fmts=fltp:channel_layouts=stereo,apad"
                          if info.has_audio else "anullsrc=r=48000:cl=stereo")
                graph.append(f"{source},atrim=duration={info.duration or 0},asetpts=PTS-STARTPTS[a{index}]")
            audio_sources = "".join(f"[a{index}]" for index in range(len(videos)))
            graph.append(f"{audio_sources}concat=n={len(videos)}:v=0:a=1[a]")
            audio_map = ["-map", "[a]"]
    video_inputs: list[str] = []
    for path in videos:
        video_inputs.extend(("-threads:v", "1" if stabilizing else "2"))
        video_inputs.extend(("-i", str(path.resolve())))
    return [*(["-init_hw_device", "opencl=stabilizer:,device_vendor=NVIDIA",
               "-filter_hw_device", "stabilizer"] if gpu_stabilization and stabilizing else []),
             *(["-vn", "-f", "concat", "-safe", "0", "-i", str(manifest)] if audio_infos is None else []),
             *video_inputs,
              "-filter_complex_threads", str(1 if gpu_stabilization else FILTER_THREADS),
              "-filter_complex", ";".join(graph), "-map", "[v]", *audio_map]


class MergeWorker:
    """Управлять FFmpeg в рабочем потоке и отправлять события главному окну."""

    def __init__(self, events: queue.SimpleQueue[tuple[str, object]], stopping: threading.Event) -> None:
        """Использовать общую очередь событий и сигнал завершения окна."""
        self.events = events
        self.stopping = stopping
        self.process: subprocess.Popen[bytes] | None = None
        self.cpu_answer = threading.Event()
        self.cpu_allowed = False

    def stop(self) -> None:
        """Остановить выполняющийся FFmpeg при закрытии приложения."""
        self.stopping.set()
        self.cpu_answer.set()
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def track_process(self, process: subprocess.Popen[bytes] | None) -> None:
        """Разрешить остановку FFmpeg как при анализе, так и при склейке."""
        self.process = process
        if process is not None and self.stopping.is_set() and process.poll() is None:
            process.kill()

    def answer_cpu_fallback(self, allowed: bool) -> None:
        """Принять решение главного потока по переходу с NVIDIA на CPU."""
        self.cpu_allowed = allowed
        journal.write(f"CPU FALLBACK user_allowed={allowed}")
        self.cpu_answer.set()

    def request_cpu_fallback(self, reason: str) -> bool:
        """Дождаться ответа окна, не выполняя CPU-обработку без согласия."""
        if self.stopping.is_set():
            return False
        self.cpu_answer.clear()
        self.cpu_allowed = False
        self.events.put(("gpu_confirmation", reason))
        journal.write("GPU FAILURE; запрос разрешения на CPU\n" + reason)
        while not self.cpu_answer.wait(0.1):
            if self.stopping.is_set():
                return False
        return self.cpu_allowed and not self.stopping.is_set()

    def analyze_stabilization(self, ffmpeg: str, videos: list[Path],
                               field_orders: list[str | None], temp_dir: str,
                               durations: list[float] | None,
                               border_crops: list[tuple[int, int, int, int] | None] | None = None,
                               metadata: list[MediaInfo] | None = None) -> bool:
        """Для каждого ролика вычислить движения и сохранить отдельный .trf.

        Анализ использует ту же обработку полей, что и финальное кодирование;
        иначе число кадров после bwdif не совпало бы с числом трансформаций.
        Рабочий каталог содержит только временные данные текущей операции.
        """
        for index, (video, order) in enumerate(zip(videos, field_orders)):
            if self.stopping.is_set():
                return False
            self.events.put(("stabilizing", (index + 1, len(videos))))
            if durations and sum(durations) > 0:
                self.events.put(("stage_progress", ("Анализ стабилизации", index + 1, len(videos), 0,
                                int(sum(durations[:index]) / sum(durations) * 100), video.name)))
            with tempfile.TemporaryFile() as errors:
                command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                     "-stats_period", "0.5", "-progress", "pipe:1",
                     "-filter_threads", str(FILTER_THREADS),
                      "-threads:v", "1", "-i", str(video.resolve()), "-vf",
                      f"{video_filter(order)}"
                      + (border_filter(border_crops[index], metadata[index]) if border_crops and metadata else "")
                      + f",vidstabdetect=result=stab-{index}.trf",
                      "-an", "-f", "null", "-"]
                journal.command(command, temp_dir)
                process = subprocess.Popen(command,
                    stdout=subprocess.PIPE, stderr=errors, cwd=temp_dir,
                    creationflags=NO_WINDOW,
                )
                self.track_process(process)
                try:
                    # Обязательно считываем pipe: иначе заполнение буфера
                    # остановит FFmpeg до окончания анализа.
                    last_percent = -1
                    assert process.stdout is not None
                    with process.stdout:
                        for line in process.stdout:
                            if not line.startswith(b"out_time=") or not durations or durations[index] <= 0:
                                continue
                            seconds = progress_seconds(line.partition(b"=")[2].decode("ascii", "replace").strip())
                            if seconds is None:
                                continue
                            percent = min(99, max(0, int(seconds / durations[index] * 100)))
                            if percent > last_percent:
                                last_percent = percent
                                self.events.put(("stabilization_progress", (index + 1, len(videos), percent)))
                                overall = int((sum(durations[:index]) + min(seconds, durations[index]))
                                              / sum(durations) * 100)
                                self.events.put(("stage_progress", ("Анализ стабилизации", index + 1,
                                                len(videos), percent, min(99, max(0, overall)), video.name)))
                    code = process.wait()
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                    self.track_process(None)
                if self.stopping.is_set():
                    return False
                errors.seek(0)
                details = errors.read().decode("utf-8", "replace").strip()
                journal.write(f"STABILIZATION EXIT {code}\n{details}")
                if code != 0:
                    raise RuntimeError(
                        f"Не удалось проанализировать стабилизацию ролика {index + 1}: "
                        "попробуйте выключить стабилизатор."
                    )
                if durations and sum(durations) > 0:
                    self.events.put(("stage_progress", ("Анализ стабилизации", index + 1, len(videos), 100,
                                    int(sum(durations[:index + 1]) / sum(durations) * 100), video.name)))
        return True

    def run_merge(self, ffmpeg: str, videos: list[Path], output: Path,
                   compress: bool = False, quality: str = "Высокое", total_seconds: float = 0,
                   use_gpu: bool = True, deinterlace: str = "Авто",
                   durations: list[float] | None = None,
                   stabilization: str = "Выключен", frame_rate: str = "Оригинал",
                   audio_quality: str = "Оригинал", resolution: str = "Как у первого ролика",
                   crop_black_border: bool = False, remove_stabilization_reflection: bool = False) -> None:
        """Склеить ролики через FFmpeg без изменения исходных файлов.

        Без «Сжать» выполняется только -c copy: без анализа, фильтрации и
        повторных попыток. С галочкой анализируем чересстрочность и
        перекодируем через NVENC/CPU; разные части обрабатываются отдельно.
        Стабилизация запускает анализ каждого ролика и затем использует
        полученные движения при кодировании, не затрагивая исходники.
        Итоговое имя получает только готовый результат, о ходе сообщаем Tk.
        """
        temporary_output: Path | None = None
        journal.write(f"MERGE START output={output}\nfiles={videos!r}\ncompress={compress}, quality={quality}, "
                      f"gpu={use_gpu}, deinterlace={deinterlace}, stabilization={stabilization}, "
                      f"frame_rate={frame_rate}, audio={audio_quality}, resolution={resolution}, "
                      f"crop_border={crop_black_border}, remove_reflection={remove_stabilization_reflection}")
        try:
            if self.stopping.is_set():
                return
            if not videos:
                raise RuntimeError("Нет видео для склейки")
            if output.resolve() in {video.resolve() for video in videos}:
                raise RuntimeError("Нельзя перезаписать исходное видео")
            if durations is not None and len(durations) != len(videos):
                raise RuntimeError("Длительности не соответствуют списку видео")
            if compress and audio_quality not in AUDIO_QUALITIES:
                raise RuntimeError("Недопустимое качество аудио для перекодирования")
            if compress and frame_rate not in FRAME_RATES:
                raise RuntimeError("Недопустимый режим частоты кадров")
            if compress and resolution not in RESOLUTIONS:
                raise RuntimeError("Недопустимое разрешение видео")
            metadata = ([probe_media(ffmpeg, video, stopping=self.stopping,
                                     track_process=self.track_process) for video in videos]
                        if compress else [])
            if self.stopping.is_set():
                return
            log_ffmpeg_version(ffmpeg)
            if compress:
                durations = [info.duration or 0 for info in metadata]
                total_seconds = sum(durations)
            auto_rates = ([auto_frame_rate(info.frame_rate) for info in metadata]
                          if compress and frame_rate == "Авто" else None)
            # Проверка «гребёнки» допускается только при явном согласии
            # пользователя на перекодирование и идёт в фоновом потоке.
            field_orders: list[str | None] = []
            if compress and deinterlace in ("Авто", "Принудительно"):
                self.events.put(("analyzing", None))
                for index, video in enumerate(videos):
                    if self.stopping.is_set():
                        return
                    order = detect_field_order(ffmpeg, video, durations[index] if durations else 0,
                                               self.stopping, self.track_process)
                    field_orders.append(order or ("auto" if deinterlace == "Принудительно" else None))
            else:
                field_orders = [None] * len(videos)
            filtering = any(field_orders)
            border_crops = [None] * len(videos)
            if compress and crop_black_border:
                for index, (video, info) in enumerate(zip(videos, metadata)):
                    self.events.put(("stage_progress", ("Определение чёрной кромки", index + 1,
                                    len(videos), 0, int(index / len(videos) * 100), video.name)))
                    border_crops[index] = detect_border(ffmpeg, video, info, stopping=self.stopping,
                                                       track_process=self.track_process)
                    self.events.put(("stage_progress", ("Определение чёрной кромки", index + 1,
                                    len(videos), 100, int((index + 1) / len(videos) * 100), video.name)))
                journal.write(f"BORDER CROPS {border_crops}")
            dimensions = output_dimensions(metadata, resolution)
            normalize = compress and (resolution != RESOLUTIONS[0] or len({
                (info.width, info.height, info.sample_aspect_ratio) for info in metadata}) > 1)
            mixed_audio = compress and audio_needs_conversion(metadata)
            journal.write(f"METADATA {metadata!r}\nfield_orders={field_orders}, output_size={dimensions}, "
                          f"normalize={normalize}, mixed_audio={mixed_audio}")
            audio_infos = metadata if compress and (mixed_audio or audio_quality != "Оригинал") else None
            if mixed_audio and audio_quality == "Оригинал" and any(info.has_audio for info in metadata):
                self.events.put(("audio_conversion", None))
            if compress:
                self.events.put(("fields_ready", field_orders))
            target_bitrate, peak_bitrate = (video_bitrate_budget(
                ffmpeg, videos, quality, metadata,
                field_orders=field_orders, auto_rates=auto_rates,
                output_size=dimensions) if compress else (0, 0))
            with tempfile.TemporaryDirectory() as temp_dir:
                if self.stopping.is_set():
                    return
                stabilizing = compress and stabilization in STABILIZATION_SMOOTHING
                # Concat demuxer читает порядок входов из временного списка.
                manifest = Path(temp_dir) / "videos.txt"
                manifest.write_text("".join(concat_entry(path) for path in videos), encoding="utf-8")
                # Выход создаётся в целевой папке для безопасной замены имени;
                # расширение указывает FFmpeg нужный формат контейнера.
                with tempfile.NamedTemporaryFile(dir=output.parent, suffix=output.suffix, delete=False) as file:
                    temporary_output = Path(file.name)
                # stderr используется только для диагностики, stdout выделен
                # под машиночитаемый -progress и читается без задержки.
                with tempfile.TemporaryFile() as errors:
                    gpu_failure: str | None = None
                    analysis_ready = False
                    for encoder in encoding_attempts(ffmpeg, compress, use_gpu, self.stopping):
                        if self.stopping.is_set():
                            return
                        if encoder == "cpu":
                            if use_gpu and not self.request_cpu_fallback(
                                    gpu_failure or "NVIDIA NVENC недоступен"):
                                if not self.stopping.is_set():
                                    raise RuntimeError("Обработка на CPU отменена пользователем.")
                                return
                            # Для GPU-пути анализ .trf не нужен. После согласия
                            # на CPU-повтор рассчитываем движения только один раз.
                        cpu_stabilization = stabilizing and (encoder == "cpu" or remove_stabilization_reflection)
                        if encoder == "nvenc" and cpu_stabilization:
                            journal.write("MODE stabilization=CPU (black borders), encoding=NVIDIA NVENC")
                        if cpu_stabilization and not analysis_ready:
                            if not self.analyze_stabilization(
                                    ffmpeg, videos, field_orders, temp_dir, durations, border_crops, metadata):
                                return
                            analysis_ready = True
                        # CPU-повтор должен заново собрать граф без CUDA:
                        # иначе ошибка GPU-фильтра повторится и на libx264.
                        graph_rate = None
                        if encoder == "nvenc" and stabilizing and not cpu_stabilization and not any(auto_rates or []):
                            # bwdif выдаёт два кадра на один чересстрочный кадр.
                            rates = [str(float(info.frame_rate) * (2 if order else 1))
                                     if info.frame_rate else None
                                     for info, order in zip(metadata, field_orders)]
                            if rates[0] and all(rate == rates[0] for rate in rates):
                                graph_rate = rates[0]
                        inputs = (filtered_inputs(
                            manifest, videos, field_orders,
                            stabilization if stabilizing else "Выключен",
                             gpu_deinterlace=encoder == "nvenc" and filtering,
                              gpu_stabilization=encoder == "nvenc" and stabilizing and not cpu_stabilization,
                              frame_rate=graph_rate, auto_rates=auto_rates,
                              output_size=dimensions if normalize else None,
                               metadata=metadata, audio_infos=audio_infos,
                               border_crops=border_crops, remove_reflection=remove_stabilization_reflection,
                         ) if compress else
                            ["-f", "concat", "-safe", "0", "-i", str(manifest)])
                        errors.seek(0)
                        errors.truncate()
                        # Пик VBR не выше 1,5 целевого и исходного среднего;
                        # небольшой буфер оставляет запас для сложных сцен.
                        # «Оригинал» копирует звуковые пакеты без преобразования.
                        audio_args = (["-c:a", "copy"] if encoder == "copy" or (audio_quality == "Оригинал" and not mixed_audio) else
                                       ["-c:a", "aac", "-b:a", "192k" if audio_quality == "Оригинал" else audio_quality.split()[1] + "k"])
                        if encoder == "nvenc":
                            rate_hint = rate_control_frame_rate(metadata, field_orders, auto_rates)
                            codec_args = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                                           "-b:v", str(target_bitrate), "-maxrate", str(peak_bitrate),
                                            "-bufsize", str(peak_bitrate * 2), "-pix_fmt", "yuv420p", *audio_args]
                            if rate_hint and nvenc_rate_hint_supported(ffmpeg):
                                codec_args += ["-rc_framerate", rate_hint]
                                journal.write(f"NVENC rate-control frame rate: {rate_hint}")
                        elif encoder == "cpu":
                            rate_hint = rate_control_frame_rate(metadata, field_orders, auto_rates)
                            codec_args = ["-c:v", "libx264", "-b:v", str(target_bitrate),
                                           "-maxrate", str(peak_bitrate), "-bufsize", str(peak_bitrate * 2),
                                           "-preset", "slow",
                                           "-pix_fmt", "yuv420p", *audio_args]
                            if rate_hint:
                                # Меняем только расчёты x264, сохраняя VFR и
                                # микросекундные PTS без фильтра fps или -r.
                                codec_args += ["-x264-params", f"fps={rate_hint}"]
                                journal.write(f"x264 rate-control frame rate: {rate_hint}")
                        else:
                            codec_args = ["-c", "copy"]
                        if encoder != "copy":
                            self.events.put(("encoding", encoder))
                            self.events.put(("processing_mode", "Стабилизация: CPU, кодирование: NVIDIA"
                                             if encoder == "nvenc" and cpu_stabilization else ""))
                        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                             "-stats_period", "0.25", "-progress", "pipe:1",
                              *inputs,
                                   *codec_args, *(["-fps_mode", "vfr", "-enc_time_base", "filter"]
                                                  if compress else []),
                               str(temporary_output)]
                        journal.command(command, temp_dir)
                        process = subprocess.Popen(command,
                            stdout=subprocess.PIPE,
                            stderr=errors,
                            cwd=temp_dir,
                            creationflags=NO_WINDOW,
                        )
                        self.track_process(process)
                        try:
                            if self.stopping.is_set():
                                process.kill()
                            # Иначе полный буфер stdout заблокирует FFmpeg.
                            last_percent = -1
                            last_position = None
                            assert process.stdout is not None
                            with process.stdout:
                                for line in process.stdout:
                                    if not line.startswith(b"out_time=") or total_seconds <= 0:
                                        continue
                                    seconds = progress_seconds(line.partition(b"=")[2].decode("ascii", "replace").strip())
                                    if seconds is None:
                                        continue
                                    if durations:
                                        position = progress_position(seconds, durations)
                                        if position != last_position:
                                            last_position = position
                                            index, local, overall = position
                                            stage = "Склейка" if encoder == "copy" else "Перекодирование"
                                            self.events.put(("stage_progress", (stage, index, len(videos),
                                                            local, overall, videos[index - 1].name)))
                                    # До успешного сохранения не показываем 100 %.
                                    percent = min(99, max(0, int(seconds / total_seconds * 100)))
                                    if percent > last_percent:
                                        last_percent = percent
                                        label = "Склейка" if encoder == "copy" else (
                                            "Перекодирование (NVIDIA)" if encoder == "nvenc"
                                            else "Перекодирование (процессор)")
                                        self.events.put(("merge_progress", (label, percent)))
                            code = process.wait()
                        finally:
                            if process.poll() is None:
                                process.kill()
                            process.wait()
                            self.track_process(None)
                        if self.stopping.is_set():
                            return
                        errors.seek(0)
                        details = errors.read().decode("utf-8", errors="replace").strip()
                        journal.write(f"ENCODER {encoder} EXIT {code}\n{details}")
                        if code == 0:
                            break
                        # При отказе GPU повторяем только явное сжатие на CPU;
                        # ошибки копирования не меняют выбранный режим.
                        if encoder == "nvenc":
                            gpu_failure = details or f"FFmpeg завершился с кодом {code}"
                            continue
                        if encoder == "copy" and (
                            "Could not find tag for codec" in details
                            or "codec not currently supported in container" in details
                        ):
                            raise RuntimeError(
                                "Исходный кодек не поддерживается выбранным форматом без перекодирования. "
                                "Выберите MKV или включите «Сжать»."
                            )
                        raise RuntimeError("Не удалось обработать видео. Проверьте входные файлы и настройки обработки.")
                # Источники остаются нетронутыми: готовый временный результат
                # переносится только после успеха FFmpeg.
                temporary_output.replace(output)
                journal.write(f"MERGE SUCCESS encoder={encoder} output={output} size={output.stat().st_size}")
                self.events.put(("merge_done", output))
        except subprocess.TimeoutExpired:
            journal.write("MERGE TIMEOUT", exception=True)
            if not self.stopping.is_set():
                self.events.put(("merge_error", "Превышено время чтения метаданных видео (30 секунд)"))
        except (OSError, RuntimeError, ValueError) as exc:
            journal.write("MERGE FAILED", exception=True)
            if not self.stopping.is_set():
                self.events.put(("merge_error", str(exc)))
        except Exception:
            journal.write("MERGE UNEXPECTED ERROR", exception=True)
            if not self.stopping.is_set():
                self.events.put(("merge_error", "Не удалось завершить обработку видео из-за внутренней ошибки."))
        finally:
            # При прерывании незавершённый выход удаляется.
            if temporary_output is not None:
                try:
                    temporary_output.unlink(missing_ok=True)
                except OSError:
                    journal.write("Не удалось удалить временный результат", exception=True)
