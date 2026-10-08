"""Главное окно: настройки, команды склейки и события фоновых задач."""

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import cast

from tkinterdnd2 import TkinterDnD

from ffmpeg import ffmpeg_path, nvenc_available
from merge import QUALITY_BITRATE, MergeWorker, estimate_output_size
from settings import AUDIO_QUALITIES, FRAME_RATES, RESOLUTIONS, AppSettings, config_path, load_settings, save_settings
from video_list import VideoListMixin
from debug_log import journal


class VideoGlue(VideoListMixin, TkinterDnD.Tk):
    """Окно приложения: список роликов, настройки вывода и ход обработки."""

    def __init__(self) -> None:
        """Создать интерфейс и подготовить обмен событиями с потоками."""
        super().__init__()
        journal.write("APPLICATION START")
        self.saved_settings = load_settings()
        self.last_input_dir = Path(self.saved_settings.input_dir) if self.saved_settings.input_dir else None
        self.last_output_dir = Path(self.saved_settings.output_dir) if self.saved_settings.output_dir else None
        # Выбор пользователя хранится отдельно от временно снятой галочки:
        # без «Сжать» NVIDIA недоступна, но предпочтение не теряется.
        self.gpu_preference = self.saved_settings.use_gpu
        self.save_job: str | None = None
        self.poll_job: str | None = None
        self.estimate_job: str | None = None
        self.title("Склейка видео")
        geometry = f"{self.saved_settings.width}x{self.saved_settings.height}"
        if self.saved_settings.x is not None and self.saved_settings.y is not None:
            geometry += f"{self.saved_settings.x:+d}{self.saved_settings.y:+d}"
        self.geometry(geometry)
        self.minsize(850, 520)
        # Только главный поток обновляет Tk. Сканирование и склейка помещают
        # результаты в очередь, а stopping завершает их при закрытии окна.
        self.events: queue.SimpleQueue[tuple[str, object]] = queue.SimpleQueue()
        self.stopping = threading.Event()
        self.worker = MergeWorker(self.events, self.stopping)
        self.busy = False
        # До фоновой проверки видеокарты запуск склейки заблокирован.
        self.gpu_checked = False
        self.gpu_available = False
        # После операции показываем фактический режим и причину CPU-повтора.
        self.last_encoder = "copy"
        self.processing_mode = ""
        self.gpu_fallback_reason: str | None = None
        self.output_path: Path | None = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.init_video_list()

        # Галочки расположены над двумя группами: параметры видео/аудио слева,
        # обработка изображения справа. Без «Сжать» фильтры недоступны.
        settings = ttk.Frame(self, padding=(12, 8))
        settings.grid(row=2, column=0, sticky="ew", padx=12, pady=(10, 0))
        settings.columnconfigure(0, weight=1)
        settings.columnconfigure(1, weight=1)
        self.format_var = tk.StringVar(value=self.saved_settings.format)
        self.compress_var = tk.BooleanVar(value=self.saved_settings.compress)
        self.quality_var = tk.StringVar(value=self.saved_settings.quality)
        self.frame_rate_var = tk.StringVar(value=self.saved_settings.frame_rate)
        self.audio_quality_var = tk.StringVar(value=self.saved_settings.audio_quality)
        self.resolution_var = tk.StringVar(value=self.saved_settings.resolution)
        self.deinterlace_var = tk.StringVar(value=self.saved_settings.deinterlace)
        self.stabilization_var = tk.StringVar(value=self.saved_settings.stabilization)
        self.crop_black_border_var = tk.BooleanVar(value=self.saved_settings.crop_black_border)
        self.remove_reflection_var = tk.BooleanVar(value=self.saved_settings.remove_stabilization_reflection)
        self.gpu_var = tk.BooleanVar(value=False)
        flags = ttk.Frame(settings)
        flags.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.compress_check = ttk.Checkbutton(flags, text="Сжать", variable=self.compress_var)
        self.compress_check.grid(row=0, column=0, padx=(0, 20))
        self.gpu_check = ttk.Checkbutton(flags, text="Использовать NVIDIA (NVENC)",
                                         variable=self.gpu_var, state="disabled")
        self.gpu_check.grid(row=0, column=1)
        encoding = ttk.LabelFrame(settings, text="Настройки перекодирования", padding=(10, 8))
        encoding.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        encoding.columnconfigure(1, weight=1)
        features = ttk.LabelFrame(settings, text="Фичи", padding=(10, 8))
        features.grid(row=1, column=1, sticky="nsew", padx=(6, 0))
        features.columnconfigure(1, weight=1)

        ttk.Label(encoding, text="Формат:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.format_box = ttk.Combobox(encoding, textvariable=self.format_var,
                                       values=("MP4", "MKV"), state="readonly", width=19)
        self.format_box.grid(row=0, column=1, sticky="ew")
        ttk.Label(encoding, text="Качество видео:").grid(row=1, column=0, sticky="w", pady=(8, 0), padx=(0, 8))
        self.quality_box = ttk.Combobox(encoding, textvariable=self.quality_var,
                                         values=tuple(QUALITY_BITRATE),
                                        state="readonly" if self.compress_var.get() else "disabled", width=19)
        self.quality_box.grid(row=1, column=1, sticky="ew", pady=(8, 0))
        ttk.Label(encoding, text="Частота кадров:").grid(row=2, column=0, sticky="w", pady=(8, 0), padx=(0, 8))
        self.frame_rate_box = ttk.Combobox(encoding, textvariable=self.frame_rate_var,
                                            values=FRAME_RATES,
                                           state="readonly" if self.compress_var.get() else "disabled", width=19)
        self.frame_rate_box.grid(row=2, column=1, sticky="ew", pady=(8, 0))
        ttk.Label(encoding, text="Качество аудио:").grid(row=3, column=0, sticky="w", pady=(8, 0), padx=(0, 8))
        self.audio_quality_box = ttk.Combobox(
            encoding, textvariable=self.audio_quality_var, values=AUDIO_QUALITIES,
            state="readonly" if self.compress_var.get() else "disabled", width=19)
        self.audio_quality_box.grid(row=3, column=1, sticky="ew", pady=(8, 0))
        # Авто анализирует реальные кадры: метка progressive бывает ошибочной.
        ttk.Label(features, text="Устранение горизонтальных линий:").grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        self.deinterlace_box = ttk.Combobox(
            features, textvariable=self.deinterlace_var,
            values=("Авто", "Принудительно", "Выключено"),
            state="readonly" if self.compress_var.get() else "disabled", width=15)
        self.deinterlace_box.grid(row=0, column=1, sticky="ew")
        # Стабилизация требует перекодирования, поэтому без «Сжать» сохраняем
        # выбор, но не разрешаем его использовать.
        ttk.Label(features, text="Программный стабилизатор:").grid(
            row=1, column=0, sticky="w", pady=(8, 0), padx=(0, 8))
        self.stabilization_box = ttk.Combobox(
            features, textvariable=self.stabilization_var,
            values=("Выключен", "Слабый", "Средний", "Сильный"),
            state="readonly" if self.compress_var.get() else "disabled", width=15)
        self.stabilization_box.grid(row=1, column=1, sticky="ew", pady=(8, 0))
        self.crop_border_check = ttk.Checkbutton(
            features, text="Обрезать чёрную кромку", variable=self.crop_black_border_var)
        self.crop_border_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.remove_reflection_check = ttk.Checkbutton(
            features, text="Убрать отражение стабилизации", variable=self.remove_reflection_var)
        self.remove_reflection_check.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(encoding, text="Разрешение:").grid(
            row=4, column=0, sticky="w", pady=(8, 0), padx=(0, 8))
        self.resolution_box = ttk.Combobox(
            encoding, textvariable=self.resolution_var, values=RESOLUTIONS,
            state="readonly" if self.compress_var.get() else "disabled", width=19)
        self.resolution_box.grid(row=4, column=1, sticky="ew", pady=(8, 0))
        self.stabilization_var.trace_add("write", self.refresh_border_controls)
        self.refresh_border_controls()
        # При копировании выбор NVIDIA и деинтерлейсинга сохраняется, но
        # становится недоступным до следующего включения сжатия.
        self.compress_var.trace_add("write", self.on_compress_changed)
        self.gpu_var.trace_add("write", self.on_gpu_changed)
        for setting in (self.format_var, self.quality_var, self.frame_rate_var, self.audio_quality_var,
                         self.deinterlace_var,
                           self.stabilization_var, self.resolution_var,
                           self.crop_black_border_var, self.remove_reflection_var):
            setting.trace_add("write", self.schedule_save)
            setting.trace_add("write", self.schedule_estimate)
        self.refresh_frame_rate_options()

        bottom = ttk.Frame(self, padding=12)
        bottom.grid(row=3, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value="Добавьте один или несколько видеофайлов")
        self.progress = ttk.Progressbar(bottom, mode="determinate", value=0)
        self.progress_label_var = tk.StringVar(value="Текущий этап")
        ttk.Label(bottom, textvariable=self.progress_label_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.progress.grid(row=3, column=0, columnspan=2, sticky="ew")
        self.overall_label_var = tk.StringVar(value="Общий прогресс")
        self.overall_label = ttk.Label(bottom, textvariable=self.overall_label_var)
        self.overall_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.overall_progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.overall_progress.grid(row=5, column=0, columnspan=2, sticky="ew")
        self.prepare_progress(1)
        # Общая панель действий находится под обеими шкалами прогресса.
        actions = ttk.Frame(bottom)
        actions.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.output_button = ttk.Button(
            actions, text="Открыть папку результата", command=self.open_output_folder,
            state="disabled", padding=(12, 6),
        )
        self.output_button.grid(row=0, column=0, padx=(0, 10))
        self.merge_button = ttk.Button(
            actions, text="Склеить", command=self.merge, state="disabled", padding=(20, 6),
        )
        self.merge_button.grid(row=0, column=1)

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Configure>", self.schedule_save)
        self.poll_job = self.after(100, self.poll_events)
        # Создаём конфиг при первом запуске и впоследствии сохраняем изменения
        # с задержкой, не записывая файл на каждое движение окна.
        if not config_path().exists():
            self.persist_settings()
        threading.Thread(target=self.check_gpu, daemon=True).start()

    def check_gpu(self) -> None:
        """Проверить NVENC в фоне и передать результат главному потоку."""
        try:
            available = nvenc_available(ffmpeg_path(), stopping=self.stopping)
        except OSError:
            available = False
        if not self.stopping.is_set():
            self.events.put(("gpu_ready", available))

    def report_callback_exception(self, exc_type: type, value: Exception, tb: object) -> None:
        """Сохранить ошибки Tk-колбэков и показать понятное сообщение."""
        import traceback
        journal.write("GUI CALLBACK ERROR\n" + "".join(traceback.format_exception(exc_type, value, tb)))
        messagebox.showerror("Ошибка интерфейса", "Не удалось выполнить действие.\n\n" + journal.notice(), parent=self)

    def prepare_progress(self, count: int) -> None:
        """Показывать общий прогресс только при обработке нескольких файлов."""
        self.progress.configure(maximum=100, value=0)
        self.overall_progress.configure(value=0)
        for widget in (self.overall_label, self.overall_progress):
            widget.grid() if count > 1 else widget.grid_remove()

    def schedule_estimate(self, *_args: object) -> None:
        """Сгруппировать изменения настроек и списка для быстрого расчёта размера."""
        if self.estimate_job is not None:
            self.after_cancel(self.estimate_job)
        self.estimate_job = self.after(150, self.refresh_estimate)

    def refresh_estimate(self, field_orders: list[str | None] | None = None) -> None:
        """Рассчитать размер только по метаданным, полученным при добавлении файлов."""
        if self.estimate_job is not None:
            self.after_cancel(self.estimate_job)
            self.estimate_job = None
        if not self.videos:
            self.estimated_size_var.set("Примерный размер: —")
            return
        infos = [self.video_metadata.get(path) for path in self.videos]
        if any(info is None for info in infos):
            self.estimated_size_var.set("Примерный размер: недостаточно метаданных")
            return
        preliminary = field_orders is None
        if field_orders is None and self.deinterlace_var.get() != "Выключено":
            field_orders = ["auto" if self.deinterlace_var.get() == "Принудительно" or info.interlaced
                            else None for info in infos]
        try:
            size = estimate_output_size(self.videos, infos, self.compress_var.get(), self.quality_var.get(),
                                        self.frame_rate_var.get(), self.audio_quality_var.get(),
                                        self.resolution_var.get(), field_orders)
        except (RuntimeError, ValueError, OSError):
            self.estimated_size_var.set("Примерный размер: недостаточно метаданных")
            return
        unit, divider = ("ГиБ", 1024 ** 3) if size >= 1024 ** 3 else ("МиБ", 1024 ** 2)
        suffix = " (предварительно до анализа)" if preliminary and self.compress_var.get() and self.deinterlace_var.get() == "Авто" else ""
        self.estimated_size_var.set(f"Примерный размер: ≈ {size / divider:.1f} {unit}{suffix}")

    def show_stage_progress(self, data: object) -> None:
        """Отобразить позицию текущего ролика и всего этапа."""
        stage, index, count, local, overall, name = data
        self.progress.configure(maximum=100, value=local)
        self.overall_progress.configure(value=overall)
        self.progress_label_var.set(f"{stage}: ролик {index} из {count} — {name} ({local}%)")
        if self.processing_mode and stage == "Перекодирование":
            self.progress_label_var.set(self.progress_label_var.get() + f" · {self.processing_mode}")
        self.overall_label_var.set(f"Общий прогресс: {stage.lower()} — {overall}%")

    def on_compress_changed(self, *_args: object) -> None:
        """Включить настройки перекодирования только вместе со сжатием."""
        compress = self.compress_var.get()
        self.gpu_var.set(compress and self.gpu_available and self.gpu_preference)
        self.quality_box.configure(state="readonly" if compress and not self.busy else "disabled")
        self.frame_rate_box.configure(state="readonly" if compress and not self.busy else "disabled")
        self.audio_quality_box.configure(state="readonly" if compress and not self.busy else "disabled")
        self.resolution_box.configure(state="readonly" if compress and not self.busy else "disabled")
        self.deinterlace_box.configure(state="readonly" if compress and not self.busy else "disabled")
        self.stabilization_box.configure(state="readonly" if compress and not self.busy else "disabled")
        self.gpu_check.configure(state="normal" if compress and self.gpu_available and not self.busy else "disabled")
        self.refresh_border_controls()
        self.schedule_save()
        self.schedule_estimate()

    def refresh_border_controls(self, *_args: object) -> None:
        """Сохранять предпочтения, блокируя недоступные режимы обработки краёв."""
        available = self.compress_var.get() and not self.busy
        self.crop_border_check.configure(state="normal" if available else "disabled")
        self.remove_reflection_check.configure(
            state="normal" if available and self.stabilization_var.get() != "Выключен" else "disabled")

    def refresh_frame_rate_options(self) -> None:
        """Показывать два режима независимо от частоты выбранных роликов."""
        self.frame_rate_box.configure(values=FRAME_RATES)
        if self.frame_rate_var.get() not in FRAME_RATES:
            self.frame_rate_var.set("Оригинал")

    def on_gpu_changed(self, *_args: object) -> None:
        """Запоминать ручной выбор NVIDIA только когда галочка доступна."""
        if self.compress_var.get() and self.gpu_checked and self.gpu_available and not self.busy:
            self.gpu_preference = self.gpu_var.get()
            self.schedule_save()

    def schedule_save(self, *_args: object) -> None:
        """Объединить частые изменения поля или геометрии в одну запись."""
        if _args and isinstance(_args[0], tk.Event) and _args[0].widget is not self:
            return
        if self.save_job is not None:
            self.after_cancel(self.save_job)
        self.save_job = self.after(500, self.persist_settings)

    def persist_settings(self) -> None:
        """Сохранить настройки с учётом текущего размера и положения окна."""
        # Метод вызывается и напрямую при закрытии; отменяем ожидающий
        # автосейв, чтобы он не обратился к уже уничтоженному окну.
        if self.save_job is not None:
            self.after_cancel(self.save_job)
            self.save_job = None
        mapped = bool(self.winfo_ismapped())
        width = max(850, self.winfo_width()) if mapped else self.saved_settings.width
        height = max(520, self.winfo_height()) if mapped else self.saved_settings.height
        self.saved_settings = AppSettings(
            input_dir=str(self.last_input_dir) if self.last_input_dir else "",
            output_dir=str(self.last_output_dir) if self.last_output_dir else "",
            format=self.format_var.get(), compress=self.compress_var.get(),
            quality=self.quality_var.get(), audio_quality=self.audio_quality_var.get(),
            resolution=self.resolution_var.get(),
            deinterlace=self.deinterlace_var.get(),
            frame_rate=self.frame_rate_var.get(),
            stabilization=self.stabilization_var.get(),
            crop_black_border=self.crop_black_border_var.get(),
            remove_stabilization_reflection=self.remove_reflection_var.get(),
            use_gpu=self.gpu_preference,
            width=width, height=height,
            x=self.winfo_x() if mapped else self.saved_settings.x,
            y=self.winfo_y() if mapped else self.saved_settings.y,
        )
        try:
            save_settings(self.saved_settings)
        except OSError as exc:
            self.status.set(f"Не удалось сохранить настройки: {exc}")

    def set_busy(self, busy: bool) -> None:
        """Заблокировать входы и настройки на время фоновой операции."""
        self.busy = busy
        self.choose_button.configure(state="disabled" if busy else "normal")
        self.merge_button.configure(state="normal" if not busy and self.gpu_checked and self.videos else "disabled")
        self.format_box.configure(state="disabled" if busy else "readonly")
        self.deinterlace_box.configure(state="readonly" if not busy and self.compress_var.get() else "disabled")
        self.stabilization_box.configure(state="readonly" if not busy and self.compress_var.get() else "disabled")
        self.compress_check.configure(state="disabled" if busy else "normal")
        # Не сбрасываем вручную снятую пользователем галочку NVIDIA.
        self.quality_box.configure(state="readonly" if not busy and self.compress_var.get() else "disabled")
        self.frame_rate_box.configure(state="readonly" if not busy and self.compress_var.get() else "disabled")
        self.audio_quality_box.configure(state="readonly" if not busy and self.compress_var.get() else "disabled")
        self.resolution_box.configure(state="readonly" if not busy and self.compress_var.get() else "disabled")
        self.gpu_check.configure(state="normal" if not busy and self.compress_var.get() and self.gpu_available else "disabled")
        self.refresh_border_controls()

    def open_output_folder(self, _event: tk.Event | None = None) -> None:
        """Открыть папку выбранного результата в файловом менеджере ОС."""
        if self.output_path is None:
            return
        folder = self.output_path.parent
        try:
            if not folder.is_dir():
                raise FileNotFoundError(f"Папка не найдена: {folder}")
            if sys.platform == "win32":
                os.startfile(folder)
            else:
                command = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.Popen([command, str(folder)], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        except OSError as exc:
            messagebox.showerror("Не удалось открыть папку", str(exc), parent=self)

    def merge(self) -> None:
        """Выбрать выходной файл и запустить склейку с настройками окна."""
        if not self.videos or not self.gpu_checked:
            return
        extension = "." + self.format_var.get().lower()
        compress = self.compress_var.get()
        # В режиме копирования GPU вообще не используется; при сжатии
        # учитываем выбор пользователя между NVIDIA и процессором.
        use_gpu = compress and self.gpu_available and self.gpu_var.get()
        quality = self.quality_var.get()
        self.refresh_frame_rate_options()
        frame_rate = self.frame_rate_var.get()
        if not compress:
            infos = [self.video_metadata.get(path) for path in self.videos]
            if all(info is not None for info in infos) and len({
                    (info.video_codec, info.width, info.height, info.has_audio, info.audio_signature)
                    for info in infos}) > 1:
                messagebox.showerror("Нужно перекодирование", "Выбранные ролики имеют разные параметры видео или звука.\n"
                                     "Включите «Сжать» для их объединения.", parent=self)
                return
        destination = filedialog.asksaveasfilename(
            parent=self,
            title="Сохранить склеенное видео",
            initialdir=str(self.last_output_dir if self.last_output_dir and self.last_output_dir.is_dir()
                           else self.videos[0].parent),
            initialfile="merged" + extension,
            defaultextension=extension,
            filetypes=[(self.format_var.get(), "*" + extension)],
        )
        if not destination:
            return
        self.last_output_dir = Path(destination).parent
        self.schedule_save()
        # Расширение определяется выбранным контейнером, даже если в диалоге
        # пользователь вручную указал другое.
        output = Path(destination).with_suffix(extension)
        if output.resolve() in {path.resolve() for path, _duration in self.items}:
            messagebox.showerror("Ошибка", "Нельзя перезаписать исходное видео", parent=self)
            return
        self.output_path = output.resolve()
        self.output_path_var.set(f"Итоговый файл: {self.output_path.name}")
        self.output_button.configure(state="normal")
        try:
            ffmpeg = ffmpeg_path()
        except FileNotFoundError as exc:
            messagebox.showerror("Ошибка", str(exc), parent=self)
            return
        self.set_busy(True)
        self.last_encoder = "copy"
        self.processing_mode = ""
        self.gpu_fallback_reason = None
        self.prepare_progress(len(self.videos))
        self.status.set("Подготовка к перекодированию…" if compress else "Склейка без преобразования…")
        # Сумма входных длительностей служит знаменателем для out_time.
        durations = {path: duration for path, duration in self.items}
        video_durations = [durations[path] for path in self.videos]
        total_seconds = sum(video_durations)
        threading.Thread(target=self.worker.run_merge,
                          args=(ffmpeg, self.videos[:], output, compress, quality, total_seconds,
                                 use_gpu, self.deinterlace_var.get(), video_durations,
                                   self.stabilization_var.get(), frame_rate, self.audio_quality_var.get(),
                                    self.resolution_var.get(), self.crop_black_border_var.get(),
                                    self.remove_reflection_var.get()),
                          daemon=True).start()

    def poll_events(self) -> None:
        """Применить события фоновых задач к виджетам в главном потоке Tk."""
        if self.poll_job is not None:
            self.after_cancel(self.poll_job)
            self.poll_job = None
        # Большая пачка файлов не должна задерживать прокрутку и закрытие.
        deadline = time.monotonic() + 0.01
        for _ in range(100):
            if time.monotonic() >= deadline:
                break
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "gpu_ready":
                self.gpu_available = bool(data)
                self.gpu_checked = True
                self.gpu_var.set(self.gpu_available and self.compress_var.get() and self.gpu_preference)
                self.set_busy(self.busy)
            elif kind == "row":
                self.add_scanned_row(*data)
            elif kind == "scan_done":
                self.finish_scan()
            elif kind == "analyzing":
                self.status.set("Проверка видео на чересстрочность…")
            elif kind == "fields_ready":
                self.refresh_estimate(data)
            elif kind == "audio_conversion":
                self.status.set("Разные аудиодорожки будут объединены в AAC 192 кбит/с")
            elif kind == "stabilizing":
                index, total = cast(tuple[int, int], data)
                self.progress.configure(value=0)
                self.status.set(f"Анализ стабилизации: ролик {index} из {total}…")
            elif kind == "stabilization_progress":
                index, total, percent = cast(tuple[int, int, int], data)
                self.progress.configure(value=percent)
                self.status.set(f"Анализ стабилизации: ролик {index} из {total}, {percent}%")
            elif kind == "encoding":
                # Новая попытка начинает заполнять прогресс с нуля.
                self.last_encoder = str(data)
                self.progress.configure(value=0)
                self.overall_progress.configure(value=0)
                self.progress_label_var.set("Перекодирование: подготовка…")
                self.overall_label_var.set("Общий прогресс: перекодирование — 0%")
                if data == "nvenc":
                    self.status.set("Перекодирование на NVIDIA…")
                else:
                    self.status.set("Перекодирование на процессоре…")
            elif kind == "processing_mode":
                self.processing_mode = str(data)
                if self.processing_mode:
                    self.progress_label_var.set(self.processing_mode + " — подготовка…")
            elif kind == "gpu_confirmation":
                # Диалог открывает только поток Tk; рабочий поток ждёт ответа.
                reason = str(data)
                journal.write("GUI GPU FALLBACK PROMPT\n" + reason)
                allowed = messagebox.askyesno(
                    "NVIDIA недоступна",
                    "Не удалось обработать видео на NVIDIA.\n"
                    "Можно повторить обработку на процессоре.\n\n" + journal.notice()
                    + "\n\nПродолжить обработку на CPU?",
                    parent=self,
                )
                if allowed:
                    self.gpu_fallback_reason = reason
                    self.status.set("Подготовка повторной обработки на процессоре…")
                self.worker.answer_cpu_fallback(allowed)
            elif kind == "merge_progress":
                label, percent = cast(tuple[str, int], data)
                self.status.set(f"{label}: {percent}%")
            elif kind == "stage_progress":
                self.show_stage_progress(data)
            elif kind == "merge_done":
                self.progress.configure(mode="determinate", maximum=100, value=100)
                self.overall_progress.configure(value=100)
                self.progress_label_var.set("Готово — 100%")
                self.overall_label_var.set("Общий прогресс — 100%")
                self.set_busy(False)
                method = {"copy": "без перекодирования", "nvenc": "NVIDIA NVENC",
                          "cpu": "процессор (libx264)"}[self.last_encoder]
                self.status.set(f"Готово ({method}): {data}")
                self.output_path = Path(cast(Path, data)).resolve()
                self.output_path_var.set(f"Итоговый файл: {self.output_path.name}")
                self.output_button.configure(state="normal")
                message = f"Видео сохранено:\n{data}\n\nСпособ обработки: {method}."
                if self.processing_mode:
                    message += "\n" + self.processing_mode + "."
                if self.gpu_fallback_reason:
                    message += "\n\nПосле сбоя NVIDIA обработка успешно завершена на процессоре."
                messagebox.showinfo("Готово", message, parent=self)
                self.open_output_folder()
            elif kind == "merge_error":
                self.progress.configure(mode="determinate", value=0)
                self.set_busy(False)
                self.status.set("Не удалось склеить видео")
                messagebox.showerror("Не удалось склеить видео", str(data) + "\n\n" + journal.notice(), parent=self)
        if not self.stopping.is_set():
            self.poll_job = self.after(10 if not self.events.empty() else 100, self.poll_events)

    def close(self) -> None:
        """Остановить фоновые задачи и завершить активный FFmpeg."""
        self.worker.stop()
        self.leave_file_drop()
        self.cancel_row_arrivals()
        self.cancel_remove_effects()
        self.cancel_icon_refresh()
        if self.estimate_job is not None:
            self.after_cancel(self.estimate_job)
            self.estimate_job = None
        if self.poll_job is not None:
            self.after_cancel(self.poll_job)
            self.poll_job = None
        if self.save_job is not None:
            self.after_cancel(self.save_job)
        self.persist_settings()
        self.destroy()
