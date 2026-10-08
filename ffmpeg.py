"""Поиск FFmpeg и проверка метаданных и доступности NVIDIA NVENC."""

import re
import shutil
import subprocess
import sys
import threading
import time
from functools import lru_cache
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from debug_log import journal


# FFmpeg сообщает длительность входного видео в stderr, а время обработки —
# в потоке, заданном через -progress pipe:1.
DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2})(?:\.(\d+))?")
TIME_PATTERN = re.compile(r"^(\d+):(\d{2}):(\d{2})(?:\.(\d+))?$")
FIELD_PATTERN = re.compile(
    r"Single frame detection:\s*TFF:\s*(\d+)\s+BFF:\s*(\d+)\s+Progressive:\s*(\d+)"
)
FRAME_RATE_PATTERN = re.compile(r"Video:.*?,\s*(\d+(?:\.\d+)?)\s+fps(?:,|\s)")
VIDEO_BITRATE_PATTERN = re.compile(r"Video:.*?,\s*(\d+)\s+kb/s(?:,|\s)")
AUDIO_BITRATE_PATTERN = re.compile(r"Audio:.*?,\s*(\d+)\s+kb/s(?:,|\s|$)")
VIDEO_SIZE_PATTERN = re.compile(r",\s*(\d{2,})x(\d{2,})(?:[,\s]|$)")
# На Windows скрываем консоль всех дочерних вызовов FFmpeg.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class MediaInfo:
    """Метаданные одного снимка файла, прочитанные одним запуском FFmpeg."""

    duration: float | None = None
    frame_rate: str | None = None
    video_bitrate: int | None = None
    has_audio: bool = False
    audio_bitrate: int | None = None
    file_size: int = 0
    width: int | None = None
    height: int | None = None
    video_codec: str = ""
    audio_signature: str = ""
    sample_aspect_ratio: float = 1.0
    interlaced: bool = False

    def estimated_video_bitrate(self, path: Path, duration: float) -> int:
        """Оценить видеобитрейт, используя известный звук вместо фиксированного резерва."""
        if self.video_bitrate:
            return self.video_bitrate
        if duration <= 0:
            raise RuntimeError(f"Не удалось определить битрейт видео: {path.name}")
        total = int(self.file_size * 8 / duration)
        # Если аудиобитрейт неизвестен, не отдаём резерву весь малый файл.
        audio = self.audio_bitrate if self.audio_bitrate is not None else min(512_000, total // 4)
        estimate = total - (audio if self.has_audio else 0)
        if estimate <= 0:
            raise RuntimeError(f"Не удалось определить битрейт видео: {path.name}")
        return estimate


# Ограниченный кэш не растёт с числом операций. Размер и временные метки
# гарантируют повторное чтение изменённого файла; ошибки в кэш не попадают.
_metadata_cache: OrderedDict[tuple[str, str, int, int, int], MediaInfo] = OrderedDict()
_nvenc_cache: OrderedDict[str, bool] = OrderedDict()
_metadata_lock = threading.Lock()


def run_diagnostic(command: list[str], timeout: float, *, stopping: threading.Event | None = None,
                   track_process: Callable[[subprocess.Popen[bytes] | None], None] | None = None) -> tuple[int, bytes]:
    """Выполнить короткую проверку FFmpeg с отменой и обязательным освобождением процесса."""
    if stopping is not None and stopping.is_set():
        raise InterruptedError("Проверка FFmpeg отменена")
    journal.command(command)
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               creationflags=NO_WINDOW)
    try:
        if track_process is not None:
            track_process(process)
        deadline = time.monotonic() + timeout
        while True:
            if stopping is not None and stopping.is_set():
                raise InterruptedError("Проверка FFmpeg отменена")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                _, diagnostic = process.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    except Exception:
        journal.write("Диагностика FFmpeg прервана", exception=True)
        raise
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stderr is not None:
            process.stderr.close()
        if track_process is not None:
            track_process(None)
    if stopping is not None and stopping.is_set():
        raise InterruptedError("Проверка FFmpeg отменена")
    journal.write(f"DIAGNOSTIC EXIT {process.returncode}\n" + diagnostic.decode("utf-8", "replace"))
    return process.returncode, diagnostic


def probe_media(ffmpeg: str, path: Path, *, stopping: threading.Event | None = None,
                track_process: Callable[[subprocess.Popen[bytes] | None], None] | None = None) -> MediaInfo:
    """Прочитать все метаданные сразу, поддерживая отмену и проверку изменения файла."""
    if stopping is not None and stopping.is_set():
        raise InterruptedError("Чтение метаданных отменено")
    resolved = path.resolve()
    stat = resolved.stat()
    key = (ffmpeg, str(resolved), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    with _metadata_lock:
        cached = _metadata_cache.get(key)
        if cached is not None:
            _metadata_cache.move_to_end(key)
            return cached
    _, diagnostic = run_diagnostic([ffmpeg, "-hide_banner", "-nostdin", "-i", str(resolved)], 30,
                                    stopping=stopping, track_process=track_process)
    output = diagnostic.decode("utf-8", "replace")
    video = next((line for line in output.splitlines() if "Video:" in line), "")
    rate = FRAME_RATE_PATTERN.search(video)
    bitrate = VIDEO_BITRATE_PATTERN.search(video)
    size = VIDEO_SIZE_PATTERN.search(video)
    audio_lines = [line for line in output.splitlines() if "Audio:" in line]
    audio_rates = [AUDIO_BITRATE_PATTERN.search(line) for line in audio_lines]
    sar = re.search(r"SAR (\d+):(\d+)", video)
    audio = audio_lines[0].partition("Audio:")[2] if audio_lines else ""
    info = MediaInfo(
        duration=duration_from_output(output) if video else None,
        frame_rate=rate.group(1) if rate else None,
        video_bitrate=int(bitrate.group(1)) * 1000 if bitrate and int(bitrate.group(1)) > 0 else None,
        has_audio=bool(audio_lines),
        audio_bitrate=sum(int(match.group(1)) * 1000 for match in audio_rates if match)
        if audio_rates and all(audio_rates) else None,
        file_size=stat.st_size,
        width=int(size.group(1)) if size else None,
        height=int(size.group(2)) if size else None,
        video_codec=video.partition("Video:")[2].split(",")[0].strip(),
        audio_signature=re.sub(r",\s*\d+\s+kb/s.*", "", audio).strip(),
        sample_aspect_ratio=int(sar.group(1)) / int(sar.group(2)) if sar and int(sar.group(2)) else 1.0,
        interlaced="bottom first" in video or "top first" in video,
    )
    # Не сохраняем сведения, если источник изменился непосредственно при чтении.
    after = resolved.stat()
    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) == key[2:]:
        with _metadata_lock:
            _metadata_cache[key] = info
            _metadata_cache.move_to_end(key)
            while len(_metadata_cache) > 256:
                _metadata_cache.popitem(last=False)
    return info


def ffmpeg_path() -> str:
    """Найти FFmpeg рядом с исходниками или внутри распакованного EXE.

    При сборке PyInstaller путь __file__ указывает внутрь временного каталога
    распаковки, где вместе с модулями находится вложенный ffmpeg.exe.
    """
    local = Path(__file__).resolve().parent / "ffmpeg.exe"
    if sys.platform == "win32" and local.is_file():
        return str(local)
    installed = shutil.which("ffmpeg")
    if installed:
        return installed
    raise FileNotFoundError("FFmpeg не найден. Положите ffmpeg.exe рядом с main.py или установите ffmpeg.")


def duration_from_output(output: str) -> float | None:
    """Извлечь длительность из диагностического вывода FFmpeg в секундах.

    Если длительности нет, файл будет отмечен в таблице как нечитабельный.
    """
    match = DURATION_PATTERN.search(output)
    if not match:
        return None
    hours, minutes, seconds, fraction = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + float(f"0.{fraction or '0'}")


def format_duration(seconds: float) -> str:
    """Представить длительность в таблице в виде ЧЧ:ММ:СС."""
    rounded = round(seconds)
    hours, remainder = divmod(rounded, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def progress_seconds(value: str) -> float | None:
    """Разобрать значение out_time из -progress, пропуская неверное время."""
    match = TIME_PATTERN.fullmatch(value)
    if not match:
        return None
    hours, minutes, seconds, fraction = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + float(f"0.{fraction or '0'}")


def probe_duration(ffmpeg: str, path: Path) -> float | None:
    """Прочитать длительность видеоролика без перекодирования.

    FFmpeg с единственным входом -i выводит сведения о потоках в stderr.
    Проверка «Video:» исключает аудиофайлы с расширением видео.
    """
    return probe_media(ffmpeg, path).duration


def probe_frame_rate(ffmpeg: str, path: Path) -> str | None:
    """Прочитать частоту входного видео для сохранения кадров после OpenCL."""
    return probe_media(ffmpeg, path).frame_rate


def probe_video_bitrate(ffmpeg: str, path: Path, duration: float) -> int:
    """Узнать средний битрейт видеопотока; при отсутствии метаданных оценить по файлу.

    Отдельный ffprobe не требуется: дистрибутив приложения включает только FFmpeg.
    Оценка по размеру учитывает аудиобитрейт; при неизвестном звуке резерв
    ограничен, чтобы не поглотить весь бюджет небольшого файла.
    """
    return probe_media(ffmpeg, path).estimated_video_bitrate(path, duration)


def detect_field_order(ffmpeg: str, path: Path, duration: float = 0,
                       stopping: threading.Event | None = None,
                       track_process: Callable[[subprocess.Popen[bytes] | None], None] | None = None) -> str | None:
    """Определить реальную чересстрочность, даже если файл помечен progressive.

    Смотрим начало и середину видео: неподвижный фрагмент не должен сам по
    себе запускать деинтерлейсинг. Неопределённые кадры не учитываются;
    требуется достаточно уверенное преобладание одного порядка полей.
    """
    detected: list[str] = []
    positions = [0.0]
    if duration > 16:
        positions.append(duration / 2)
    for position in positions:
        if stopping is not None and stopping.is_set():
            break
        try:
            code, diagnostic = run_diagnostic(
                [ffmpeg, "-hide_banner", "-loglevel", "info", "-nostdin",
                 "-ss", str(position), "-i", str(path), "-vf", "idet",
                 "-frames:v", "150", "-an", "-f", "null", "-"],
                20, stopping=stopping, track_process=track_process,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if code != 0:
            continue
        matches = FIELD_PATTERN.findall(diagnostic.decode("utf-8", "replace"))
        if matches:
            top, bottom, progressive = map(int, matches[-1])
            # Одна уверенная выборка достаточна: в начале могут быть титры,
            # а движение, раскрывающее порядок полей, появится позже.
            decisive = top + bottom + progressive
            if decisive >= 25 and max(top, bottom) >= decisive * 0.9:
                detected.append("tff" if top > bottom else "bff")
    # Если два участка имеют разный порядок, авто не должен гадать.
    if not detected or len(set(detected)) > 1:
        return None
    return detected[0]


@lru_cache(maxsize=4)
def _nvenc_rate_hint_supported(ffmpeg: str, size: int, modified: int) -> bool:
    """Кэшировать возможности конкретной версии бинарника, а не только пути."""
    command = [ffmpeg, "-hide_banner", "-h", "encoder=h264_nvenc"]
    journal.command(command)
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=5, creationflags=NO_WINDOW)
    return result.returncode == 0 and b"rc_framerate" in result.stdout


def nvenc_rate_hint_supported(ffmpeg: str) -> bool:
    """Сторонние сборки без нашей подсказки продолжают работать штатно."""
    try:
        stat = Path(ffmpeg).stat()
        return _nvenc_rate_hint_supported(ffmpeg, stat.st_size, stat.st_mtime_ns)
    except (OSError, subprocess.TimeoutExpired):
        return False


def nvenc_available(ffmpeg: str, *, stopping: threading.Event | None = None) -> bool:
    """Проверить реальное аппаратное кодирование кадра размером 256×256.

    Наличие h264_nvenc в FFmpeg само по себе не гарантирует наличие рабочего
    драйвера. Маленькие кадры некоторые карты не принимают, поэтому проверка
    выполняется на 256×256 и в рабочем потоке, а её результат кешируется.
    """
    if stopping is not None and stopping.is_set():
        raise InterruptedError("Проверка NVIDIA отменена")
    with _metadata_lock:
        if ffmpeg in _nvenc_cache:
            _nvenc_cache.move_to_end(ffmpeg)
            return _nvenc_cache[ffmpeg]
    try:
        code, _ = run_diagnostic(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-f", "lavfi", "-i", "color=c=black:s=256x256:r=25",
             "-frames:v", "1", "-c:v", "h264_nvenc", "-preset", "p5",
             "-rc", "vbr", "-cq", "17", "-b:v", "0", "-f", "null", "-"],
            15, stopping=stopping,
        )
    except InterruptedError:
        raise
    except (OSError, subprocess.TimeoutExpired):
        code = -1
    with _metadata_lock:
        _nvenc_cache[ffmpeg] = code == 0
        _nvenc_cache.move_to_end(ffmpeg)
        while len(_nvenc_cache) > 4:
            _nvenc_cache.popitem(last=False)
    return code == 0
