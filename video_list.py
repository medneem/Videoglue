"""Таблица роликов: добавление, проверка длительности и порядок файлов."""

import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from types import SimpleNamespace

from tkinterdnd2 import COPY, DND_FILES

from ffmpeg import MediaInfo, ffmpeg_path, format_duration, probe_media


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
ICON_SIZE = 20


def draw_bootstrap_icon(canvas: tk.Canvas, kind: str, color: str) -> None:
    """Нарисовать контуры Bootstrap Icons: square, check-square и x-lg.

    Источник форм: icons.getbootstrap.com/icons/square/,
    /icons/check-square/ и /icons/x-lg/ (лицензия MIT).
    """
    if getattr(canvas, "icon_state", None) == (kind, color):
        return
    canvas.icon_state = (kind, color)
    canvas.delete("all")
    if kind == "remove":
        canvas.create_line(4, 4, 16, 16, fill=color, width=2, capstyle="round")
        canvas.create_line(16, 4, 4, 16, fill=color, width=2, capstyle="round")
    else:
        # Скруглённые углы повторяют внешний контур исходных SVG.
        for start, box in ((90, (2, 2, 6, 6)), (0, (14, 2, 18, 6)),
                           (270, (14, 14, 18, 18)), (180, (2, 14, 6, 18))):
            canvas.create_arc(*box, start=start, extent=90, style="arc", outline=color, width=1.5)
        for points in ((4, 2, 16, 2), (18, 4, 18, 16),
                       (16, 18, 4, 18), (2, 16, 2, 4)):
            canvas.create_line(*points, fill=color, width=1.5)
        if kind == "checked":
            # Три опорные точки повторяют галочку в check-square.svg.
            canvas.create_line(6, 10, 9, 13, 15, 6, fill=color, width=2,
                               capstyle="round", joinstyle="round")


class VideoListMixin:
    """Управлять списком роликов и его виджетами в главном окне.

    Окно предоставляет очередь events, флаг stopping, статус и полосу
    прогресса. Такое разделение оставляет всю работу с файлами и таблицей
    вместе, а события фонового сканирования передаются в окно через очередь.
    """

    def init_video_list(self) -> None:
        """Создать таблицу, кнопку добавления файлов и модель роликов."""
        # items хранит полный порядок, включая файлы без длительности;
        # videos содержит пригодные к склейке файлы в порядке таблицы.
        self.items: list[tuple[Path, float | None]] = []
        self.videos: list[Path] = []
        self.video_frame_rates: dict[Path, str | None] = {}
        self.video_metadata: dict[Path, MediaInfo] = {}
        self.included_rows: set[str] = set()
        self.dragged_row: str | None = None
        self.drag_start_y = 0
        self.drag_target: str | None = None
        self.drag_gap: str | None = None
        self.remove_pressed_row: str | None = None
        self.removing_rows: set[str] = set()
        self.drop_pulse_job: str | None = None
        self.arrival_jobs: dict[str, str] = {}
        self.removal_jobs: dict[str, list[str]] = {}
        self.icon_refresh_job: str | None = None
        self.row_icons: dict[str, tuple[tk.Canvas, tk.Canvas]] = {}
        self.icon_pool: list[tuple[tk.Canvas, tk.Canvas]] = []

        top = ttk.Frame(self, padding=12)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        self.choose_button = ttk.Button(top, text="Добавить видео", command=self.choose_files)
        self.choose_button.grid(row=0, column=0, padx=(0, 12))
        self.folder_label = ttk.Label(top, text="Файлы не выбраны")
        self.folder_label.grid(row=0, column=1, sticky="ew")

        # Подсказка остаётся видимой и после загрузки списка.
        table = ttk.Frame(self, padding=(12, 0, 12, 0))
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(1, weight=1)
        self.drop_hint = tk.StringVar(value="↓  Перетащите сюда видео из Проводника  ·  тяните строки для изменения порядка")
        ttk.Label(table, textvariable=self.drop_hint).grid(row=0, column=0, columnspan=2,
                                                            sticky="w", pady=(0, 7))
        self.drop_surface = tk.Frame(table, background="#d7dfe9", padx=2, pady=2)
        self.drop_surface.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self.drop_surface.columnconfigure(0, weight=1)
        self.drop_surface.rowconfigure(0, weight=1)
        style = ttk.Style(self)
        style.configure("Video.Treeview", rowheight=30, font=("TkDefaultFont", 10))
        style.configure("Video.Treeview.Heading", font=("TkDefaultFont", 10, "bold"))
        self.tree = ttk.Treeview(self.drop_surface, columns=("include", "number", "name", "duration", "remove"),
                                 show="headings", style="Video.Treeview", selectmode="browse")
        self.tree.heading("include", text="", command=self.toggle_all_rows)
        self.tree.heading("number", text="Номер файла")
        self.tree.heading("name", text="Файл")
        self.tree.heading("duration", text="Длительность")
        self.tree.heading("remove", text="", command=self.clear_files)
        self.tree.column("include", width=48, minwidth=48, anchor="center", stretch=False)
        self.tree.column("number", width=105, anchor="center", stretch=False)
        self.tree.column("name", width=300)
        self.tree.column("duration", width=140, anchor="center", stretch=False)
        self.tree.column("remove", width=42, minwidth=42, anchor="center", stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.tag_configure("stripe", background="#f1f5fa")
        self.tree.tag_configure("drop_target", background="#c6e2ff", foreground="#133b68")
        self.tree.tag_configure("drop_gap", background="#dbeeff", foreground="#1873bf",
                                font=("TkDefaultFont", 10, "bold"))
        self.tree.tag_configure("arrive", background="#c9f2da", foreground="#174b32")
        self.tree.tag_configure("drag_source", background="#e4eaf2", foreground="#8a98a9")
        self.tree.tag_configure("remove_press", background="#ffcad3", foreground="#a41636",
                                font=("TkDefaultFont", 11, "bold"))
        self.tree.tag_configure("remove_fade", background="#fbe9ec", foreground="#b68590")
        scrollbar = ttk.Scrollbar(self.drop_surface, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=lambda first, last: self.on_tree_scroll(
            scrollbar, first, last))
        self.tree.bind("<Configure>", self.schedule_icon_refresh, add="+")
        self.tree.bind("<<TreeviewSelect>>", self.schedule_icon_refresh, add="+")
        heading_background = style.lookup("Treeview.Heading", "background") or "#f0f0f0"
        self.heading_icon = tk.Canvas(self.tree, width=ICON_SIZE, height=ICON_SIZE,
                                      highlightthickness=0, background=heading_background, cursor="hand2")
        self.heading_icon.place(x=(self.tree.column("include", "width") - ICON_SIZE) // 2, y=3)
        self.heading_icon.bind("<Button-1>", lambda _event: self.toggle_all_rows())
        draw_bootstrap_icon(self.heading_icon, "empty", "#375c83")
        self.clear_icon = tk.Canvas(self.tree, width=ICON_SIZE, height=ICON_SIZE,
                                    highlightthickness=0, background=heading_background, cursor="hand2")
        self.clear_icon.bind("<Button-1>", lambda _event: self.clear_files())
        draw_bootstrap_icon(self.clear_icon, "remove", "#be2437")
        self.empty_hint = tk.Label(self.tree, text="＋  Добавьте видеоролики\n\n"
                                  "Перетащите файлы сюда или добавьте видео кнопкой выше",
                                  background="#f8fbff", foreground="#405975",
                                  font=("TkDefaultFont", 11), padx=26, pady=20)
        self.empty_hint.place(relx=0.5, rely=0.5, anchor="center")
        # Плашка поверх таблицы движется вслед за захваченной строкой.
        self.drag_preview = tk.Frame(self.tree, background="#9aacc3")
        self.drag_preview_label = tk.Label(
            self.drag_preview, background="#ffffff", foreground="#173e66",
            font=("TkDefaultFont", 10, "bold"), padx=15, pady=8,
        )
        self.drag_preview_label.grid(row=0, column=0, padx=(0, 4), pady=(0, 4))
        for widget in (self.tree, self.empty_hint):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<DropEnter>>", self.enter_file_drop)
            widget.dnd_bind("<<DropPosition>>", self.enter_file_drop)
            widget.dnd_bind("<<DropLeave>>", self.leave_file_drop)
            widget.dnd_bind("<<Drop>>", self.drop_files)
        self.tree.bind("<ButtonPress-1>", self.start_row_drag)
        self.tree.bind("<B1-Motion>", self.show_row_drag)
        self.tree.bind("<ButtonRelease-1>", self.finish_row_drag)
        self.tree.bind("<Double-1>", self.open_video)
        self.selected_duration = tk.StringVar(value="Общий хронометраж выбранных видео: 00:00:00")
        summary = ttk.Frame(table)
        summary.grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        summary.columnconfigure(4, weight=1)
        ttk.Label(summary, textvariable=self.selected_duration).grid(row=0, column=0)
        ttk.Label(summary, text="  ·  ").grid(row=0, column=1)
        self.estimated_size_var = tk.StringVar(value="Примерный размер: —")
        ttk.Label(summary, textvariable=self.estimated_size_var).grid(row=0, column=2)
        ttk.Label(summary, text="  ·  ").grid(row=0, column=3)
        self.output_path_var = tk.StringVar(value="Итоговый файл: ещё не выбран")
        self.output_label = ttk.Label(summary, textvariable=self.output_path_var, width=1)
        self.output_label.grid(row=0, column=4, sticky="ew")

    def enter_file_drop(self, _event: tk.Event) -> str:
        """Подсветить область приёма и запустить мягкое мерцание её рамки."""
        if self.busy:
            return "none"
        self.drop_hint.set("↓  Отпустите файлы — добавим видео в конец списка")
        self.empty_hint.configure(text="↓  Отпустите файлы здесь", background="#e8f3ff")
        if self.drop_pulse_job is None:
            self.pulse_drop_surface(True)
        return COPY

    def pulse_drop_surface(self, bright: bool) -> None:
        """Чередовать цвета рамки, пока файлы удерживаются над таблицей."""
        self.drop_surface.configure(background="#388be0" if bright else "#a6d4ff")
        self.drop_pulse_job = self.after(280, self.pulse_drop_surface, not bright)

    def leave_file_drop(self, _event: tk.Event | None = None) -> None:
        """Вернуть подсказку и рамку в обычное состояние."""
        if self.drop_pulse_job is not None:
            self.after_cancel(self.drop_pulse_job)
            self.drop_pulse_job = None
        self.drop_surface.configure(background="#d7dfe9")
        self.drop_hint.set("↓  Перетащите сюда видео из Проводника  ·  тяните строки для изменения порядка")
        self.empty_hint.configure(text="＋  Добавьте видеоролики\n\n"
                                  "Перетащите файлы сюда или добавьте видео кнопкой выше",
                                  background="#f8fbff")

    def refresh_row_colors(self) -> None:
        """Обновить чередование строк после добавления или перестановки."""
        for index, row in enumerate(self.tree.get_children()):
            tags = tuple(tag for tag in self.tree.item(row, "tags") if tag != "stripe")
            self.tree.item(row, tags=(("stripe",) if index % 2 else ()) + tags)
        self.schedule_icon_refresh()

    def on_tree_scroll(self, scrollbar: ttk.Scrollbar, first: str, last: str) -> None:
        """После прокрутки передвинуть значки к видимым строкам."""
        scrollbar.set(first, last)
        self.schedule_icon_refresh()

    def schedule_icon_refresh(self, _event: tk.Event | None = None) -> None:
        """Сгруппировать перерисовки при загрузке и прокрутке списка."""
        if self.icon_refresh_job is None:
            self.icon_refresh_job = self.after_idle(self.refresh_row_icons)

    def icon_background(self, row: str) -> str:
        """Подобрать фон значка под цвет подсветки строки."""
        tags = self.tree.item(row, "tags")
        for tag, color in (("remove_press", "#ffcad3"), ("remove_fade", "#fbe9ec"),
                           ("drag_source", "#e4eaf2"), ("drop_target", "#c6e2ff"),
                           ("arrive", "#c9f2da"), ("stripe", "#f1f5fa")):
            if tag in tags:
                return color
        if row in self.tree.selection():
            return "#0078d7" if sys.platform == "win32" else "#4a83ba"
        return "#ffffff"

    def make_row_icon(self, row: str, column: str) -> tk.Canvas:
        """Создать накладной значок с прежним поведением ячейки таблицы."""
        icon = tk.Canvas(self.tree, width=ICON_SIZE, height=ICON_SIZE,
                         highlightthickness=0, cursor="hand2")
        icon.row = row
        if column == "include":
            # При прокрутке Canvas получает другую строку без регистрации
            # новых Tcl-колбэков; число виджетов зависит от высоты окна.
            icon.bind("<Button-1>", lambda _event: self.toggle_row(icon.row))
        else:
            icon.bind("<ButtonPress-1>", lambda event: self.start_row_drag(
                SimpleNamespace(x=event.widget.winfo_x() + event.x,
                                y=event.widget.winfo_y() + event.y)))
            icon.bind("<ButtonRelease-1>", lambda _event: self.finish_row_drag(
                SimpleNamespace(x=self.tree.winfo_pointerx() - self.tree.winfo_rootx(),
                                y=self.tree.winfo_pointery() - self.tree.winfo_rooty())))
        return icon

    def refresh_row_icons(self) -> None:
        """Показать значки только в видимых ячейках Treeview."""
        self.icon_refresh_job = None
        height = self.tree.winfo_height()
        self.clear_icon.place(x=self.tree.winfo_width() - self.tree.column("remove", "width")
                              + (self.tree.column("remove", "width") - ICON_SIZE) // 2 - 1, y=3)
        row_height = int(ttk.Style(self).lookup("Video.Treeview", "rowheight") or 30)
        # identify_row позволяет найти видимые строки без обхода всего списка.
        rows = {self.tree.identify_row(y) for y in range(0, height, max(1, row_height // 2))}
        rows.add(self.tree.identify_row(max(0, height - 1)))
        rows.discard("")
        rows.discard(self.drag_gap)
        for row in self.row_icons.keys() - rows:
            icons = self.row_icons.pop(row)
            for icon in icons:
                icon.place_forget()
            self.icon_pool.append(icons)
        for row in rows:
            boxes = (self.tree.bbox(row, "include"), self.tree.bbox(row, "remove"))
            if row not in self.row_icons:
                self.row_icons[row] = (self.icon_pool.pop() if self.icon_pool else
                                      (self.make_row_icon(row, "include"), self.make_row_icon(row, "remove")))
                for icon in self.row_icons[row]:
                    icon.row = row
            background = self.icon_background(row)
            for column, icon, box in zip(("include", "remove"), self.row_icons[row], boxes):
                if not box:
                    icon.place_forget()
                    continue
                x, y, width, height = box
                if icon.cget("background") != background:
                    icon.configure(background=background)
                kind = (("checked" if row in self.included_rows else "empty") if column == "include"
                        else "remove")
                color = "#be2437" if column == "remove" else "#375c83"
                draw_bootstrap_icon(icon, kind, color)
                icon.place(x=x + (width - ICON_SIZE) // 2,
                           y=y + (height - ICON_SIZE) // 2)
        # При уменьшении окна не оставляем большой запас скрытых виджетов.
        while len(self.icon_pool) > max(1, len(rows)):
            for icon in self.icon_pool.pop():
                icon.destroy()

    def cancel_icon_refresh(self) -> None:
        """Отменить отложенную перерисовку при закрытии окна."""
        if self.icon_refresh_job is not None:
            self.after_cancel(self.icon_refresh_job)
            self.icon_refresh_job = None

    def clear_drag_target(self) -> None:
        """Убрать подсветку и временный промежуток перед вставкой."""
        if self.drag_gap and self.tree.exists(self.drag_gap):
            self.tree.delete(self.drag_gap)
        self.drag_gap = None
        if self.drag_target and self.tree.exists(self.drag_target):
            tags = tuple(tag for tag in self.tree.item(self.drag_target, "tags") if tag != "drop_target")
            self.tree.item(self.drag_target, tags=tags)
        self.drag_target = None
        self.tree.configure(cursor="")
        self.drop_hint.set("↓  Перетащите сюда видео из Проводника  ·  тяните строки для изменения порядка")
        self.schedule_icon_refresh()

    def show_row_drag(self, event: tk.Event) -> None:
        """Показать движущуюся копию строки и подсветить место назначения."""
        if not self.dragged_row or self.busy or abs(event.y - self.drag_start_y) < 5:
            return
        source = self.dragged_row
        if not self.drag_preview.winfo_manager():
            name = self.tree.set(source, "name")
            short_name = name if len(name) <= 52 else name[:49] + "…"
            self.drag_preview_label.configure(text=f"↕  {short_name}    {self.tree.set(source, 'duration')}")
            self.tree.selection_remove(source)
            self.tree.item(source, tags=(*self.tree.item(source, "tags"), "drag_source"))
        # Ограничиваем плашку видимой частью списка, не скрывая её за границей окна.
        width = self.tree.winfo_width()
        height = self.tree.winfo_height()
        x = min(max(getattr(event, "x", 0) + 12, 4), max(4, width - self.drag_preview.winfo_reqwidth() - 4))
        y = min(max(event.y - 14, 4), max(4, height - self.drag_preview.winfo_reqheight() - 4))
        self.drag_preview.place(x=x, y=y)
        target = self.tree.identify_row(event.y)
        if target == self.drag_gap:
            target = self.drag_target
        if not target or target == self.dragged_row:
            self.clear_drag_target()
            self.tree.configure(cursor="fleur")
            self.drop_hint.set("↕  Переместите строку к нужной позиции и отпустите мышь")
            return
        if target != self.drag_target:
            self.clear_drag_target()
            source_index = self.tree.index(source)
            target_index = self.tree.index(target)
            self.drag_target = target
            self.tree.item(target, tags=(*self.tree.item(target, "tags"), "drop_target"))
            # Treeview не умеет менять высоту отдельной строки: временная строка
            # раздвигает соседние файлы именно в точке будущей вставки.
            gap_index = target_index + (source_index < target_index)
            self.drag_gap = self.tree.insert("", gap_index, values=("", "", "→  Вставить здесь", "", ""),
                                              tags=("drop_gap",))
            self.schedule_icon_refresh()
        self.tree.configure(cursor="fleur")
        position = self.tree.index(target) + (self.tree.index(source) < self.tree.index(target))
        self.drop_hint.set(f"↕  Отпустите мышь, чтобы переместить файл на позицию {position}")

    def open_video(self, event: tk.Event) -> None:
        """Открыть ролик из столбца с именем в системном видеоплеере."""
        if self.tree.identify_column(event.x) != "#3":
            return
        row = self.tree.identify_row(event.y)
        if not row or self.drag_gap:
            return
        path = self.items[self.tree.index(row)][0]
        try:
            if not path.is_file():
                raise FileNotFoundError(f"Видеофайл не найден: {path}")
            if sys.platform == "win32":
                os.startfile(path)
            else:
                command = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.Popen([command, str(path)], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        except OSError as exc:
            messagebox.showerror("Не удалось открыть видео", str(exc), parent=self)

    def drop_files(self, event: tk.Event) -> str:
        """Добавить перенесённые файлы, исключая дубликаты и другие форматы.

        Tcl splitlist разбирает пути с пробелами и фигурными скобками.
        """
        self.leave_file_drop()
        if self.busy:
            return "none"
        try:
            paths = [Path(name) for name in self.tk.splitlist(event.data)]
        except tk.TclError as exc:
            messagebox.showerror("Ошибка", str(exc), parent=self)
            return "none"
        return COPY if self.add_files(paths) else "none"

    def add_files(self, paths: list[Path]) -> bool:
        """Добавить новые видео в порядке выбора, пропуская повторы и иные файлы."""
        if self.busy:
            return False
        try:
            known = {str(path.resolve()).casefold() for path, _ in self.items}
            added: list[Path] = []
            for path in paths:
                if (path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
                        and str(path.resolve()).casefold() not in known):
                    added.append(path)
                    known.add(str(path.resolve()).casefold())
            if not added:
                self.status.set("Нет новых поддерживаемых видеофайлов")
                return False
            ffmpeg = ffmpeg_path()
        except OSError as exc:
            messagebox.showerror("Ошибка", str(exc), parent=self)
            return False
        self.last_input_dir = added[0].parent
        self.schedule_save()
        folders = {path.parent for path, _duration in self.items} | {path.parent for path in added}
        self.folder_label.configure(text=str(next(iter(folders))) if len(folders) == 1
                                    else "Файлы из разных папок")
        self.start_scan(ffmpeg, added)
        return True

    def start_row_drag(self, event: tk.Event) -> str | None:
        """Запомнить строку, с которой началось перемещение мышью."""
        if self.tree.identify_region(event.x, event.y) == "heading":
            self.dragged_row = None
            return None
        self.hide_row_preview()
        self.clear_drag_target()
        if self.remove_pressed_row and self.tree.exists(self.remove_pressed_row):
            self.clear_remove_effect(self.remove_pressed_row)
        self.remove_pressed_row = None
        if self.removing_rows:
            self.dragged_row = None
            return "break"
        if self.tree.identify_column(event.x) == "#1":
            self.dragged_row = None
            if not self.busy:
                row = self.tree.identify_row(event.y)
                if row and row != self.drag_gap:
                    self.toggle_row(row)
            return "break"
        if not self.busy and self.tree.identify_column(event.x) == "#5":
            row = self.tree.identify_row(event.y)
            if row:
                self.dragged_row = None
                if row not in self.removing_rows:
                    self.remove_pressed_row = row
                    self.tree.selection_remove(row)
                    self.tree.item(row, tags=(*self.tree.item(row, "tags"), "remove_press"))
                    self.schedule_icon_refresh()
                return "break"
        self.dragged_row = None if self.busy else self.tree.identify_row(event.y)
        self.drag_start_y = event.y

    def hide_row_preview(self) -> None:
        """Убрать плавающую плашку и восстановить цвет исходной строки."""
        self.drag_preview.place_forget()
        if self.dragged_row and self.tree.exists(self.dragged_row):
            tags = tuple(tag for tag in self.tree.item(self.dragged_row, "tags") if tag != "drag_source")
            self.tree.item(self.dragged_row, tags=tags)

    def finish_row_drag(self, event: tk.Event) -> str | None:
        """Перенести захваченную строку на место строки под курсором."""
        if self.remove_pressed_row:
            row = self.remove_pressed_row
            self.remove_pressed_row = None
            if self.tree.identify_column(event.x) == "#5" and self.tree.identify_row(event.y) == row:
                self.removing_rows.add(row)
                self.removal_jobs[row] = [self.after(90, self.fade_remove_effect, row),
                                          self.after(210, self.finish_remove_effect, row)]
            else:
                self.clear_remove_effect(row)
            return "break"
        source = self.dragged_row
        target = self.tree.identify_row(event.y)
        if target == self.drag_gap:
            target = self.drag_target
        self.hide_row_preview()
        self.dragged_row = None
        self.clear_drag_target()
        if not source or self.busy or abs(event.y - self.drag_start_y) < 5:
            return
        if not target and event.y >= self.tree.winfo_height():
            target = self.tree.get_children()[-1]
        if target and target != source:
            self.move_row(source, target)

    def clear_remove_effect(self, row: str) -> None:
        """Восстановить вид строки после отмены нажатия на крестик."""
        if self.tree.exists(row):
            tags = tuple(tag for tag in self.tree.item(row, "tags")
                         if tag not in ("remove_press", "remove_fade"))
            self.tree.item(row, tags=tags)
            self.schedule_icon_refresh()

    def fade_remove_effect(self, row: str) -> None:
        """Смягчить цвет строки перед её исчезновением."""
        if self.tree.exists(row) and row in self.removing_rows:
            self.clear_remove_effect(row)
            self.tree.item(row, tags=(*self.tree.item(row, "tags"), "remove_fade"))
            self.schedule_icon_refresh()

    def finish_remove_effect(self, row: str) -> None:
        """Завершить анимацию и удалить файл из списка."""
        if self.tree.exists(row) and (self.busy or self.drag_gap):
            job = self.after(100, self.finish_remove_effect, row)
            self.removal_jobs[row] = [job]
            return
        self.removal_jobs.pop(row, None)
        self.removing_rows.discard(row)
        self.clear_remove_effect(row)
        self.remove_row(row)

    def cancel_remove_effects(self) -> None:
        """Отменить все фазы удаления, включая ожидание окончания фоновой задачи."""
        for jobs in self.removal_jobs.values():
            for job in jobs:
                if job:
                    self.after_cancel(job)
        self.removal_jobs.clear()
        self.removing_rows.clear()

    def remove_row(self, row: str) -> None:
        """Убрать ролик из таблицы и очереди склейки, не удаляя сам файл."""
        if self.busy or self.drag_gap or not self.tree.exists(row):
            return
        path, _duration = self.items.pop(self.tree.index(row))
        self.video_frame_rates.pop(path, None)
        self.video_metadata.pop(path, None)
        self.included_rows.discard(row)
        job = self.arrival_jobs.pop(row, None)
        if job:
            self.after_cancel(job)
        self.tree.delete(row)
        for index, remaining in enumerate(self.tree.get_children(), 1):
            self.tree.set(remaining, "number", index)
        self.refresh_row_colors()
        if not self.items:
            self.empty_hint.place(relx=0.5, rely=0.5, anchor="center")
        self.update_videos()
        self.status.set(f"Убран из списка: {path.name}")

    def clear_files(self) -> None:
        """Очистить всю очередь и её таймеры, сохранив файлы на диске."""
        if self.busy:
            return
        self.cancel_row_arrivals()
        self.cancel_remove_effects()
        self.leave_file_drop()
        self.clear_drag_target()
        self.drag_preview.place_forget()
        self.dragged_row = None
        self.remove_pressed_row = None
        rows = self.tree.get_children()
        if rows:
            self.tree.delete(*rows)
        self.items.clear()
        self.videos.clear()
        self.included_rows.clear()
        self.video_frame_rates.clear()
        self.video_metadata.clear()
        self.folder_label.configure(text="Файлы не выбраны")
        self.empty_hint.place(relx=0.5, rely=0.5, anchor="center")
        self.update_videos()
        self.prepare_progress(1)
        self.progress_label_var.set("Текущий этап")
        self.status.set("Список очищен. Добавьте видеофайлы")

    def move_row(self, source: str, target: str) -> None:
        """Синхронно изменить порядок в модели и таблице, затем перенумеровать."""
        start = self.tree.index(source)
        end = self.tree.index(target)
        item = self.items.pop(start)
        self.items.insert(end, item)
        self.tree.move(source, "", end)
        self.tree.selection_remove(source)
        for index, row in enumerate(self.tree.get_children(), 1):
            self.tree.set(row, "number", index)
        self.refresh_row_colors()
        self.tree.item(source, tags=(*self.tree.item(source, "tags"), "arrive"))
        self.schedule_row_arrival(source)
        self.update_videos()

    def update_include_heading(self) -> None:
        """Показывать общий флажок, когда отмечены все строки таблицы."""
        checked = bool(self.items) and len(self.included_rows) == len(self.items)
        draw_bootstrap_icon(self.heading_icon, "checked" if checked else "empty", "#375c83")

    def toggle_row(self, row: str) -> None:
        """Переключить участие файла в склейке, сохранив его в таблице."""
        if self.busy or row == self.drag_gap or not self.tree.exists(row):
            return
        if row in self.included_rows:
            self.included_rows.remove(row)
        else:
            self.included_rows.add(row)
        self.schedule_icon_refresh()
        self.update_videos()
        self.status.set(f"Выбрано для склейки: {len(self.videos)} видео")

    def toggle_all_rows(self) -> None:
        """Отметить все файлы либо снять все отметки в заголовке."""
        if self.busy or self.drag_gap or self.removing_rows:
            return
        rows = self.tree.get_children()
        select_all = not all(row in self.included_rows for row in rows)
        self.included_rows = set(rows) if select_all else set()
        self.schedule_icon_refresh()
        self.update_videos()
        self.status.set(f"Выбрано для склейки: {len(self.videos)} видео")

    def update_videos(self) -> None:
        """Пересобрать входы из отмеченных строк с известной длительностью."""
        rows = [row for row in self.tree.get_children() if row != self.drag_gap]
        self.videos = []
        total = 0.0
        for row, (path, duration) in zip(rows, self.items):
            if row in self.included_rows and duration is not None:
                self.videos.append(path)
                total += duration
        self.selected_duration.set(f"Общий хронометраж выбранных видео: {format_duration(total)}")
        self.update_include_heading()
        self.schedule_icon_refresh()
        self.merge_button.configure(state="normal" if not self.busy and self.gpu_checked and self.videos else "disabled")
        self.schedule_estimate()

    def start_scan(self, ffmpeg: str, paths: list[Path]) -> None:
        """Запустить чтение длительностей в фоне, настроив прогресс по файлам."""
        self.prepare_progress(1)
        self.progress_label_var.set("Добавление файлов")
        self.progress.configure(mode="determinate", maximum=len(paths), value=0)
        self.set_busy(True)
        self.status.set(f"Чтение длительности: 0 из {len(paths)}")
        threading.Thread(target=self.scan, args=(ffmpeg, paths), daemon=True).start()

    def choose_files(self) -> None:
        """Открыть диалог выбора одного или нескольких видео для добавления."""
        if self.busy:
            return
        options = {"parent": self, "title": "Добавить видеофайлы",
                   "filetypes": [("Видеофайлы", " ".join("*" + ext for ext in sorted(VIDEO_EXTENSIONS))),
                                 ("Все файлы", "*.*")]}
        if self.last_input_dir and self.last_input_dir.is_dir():
            options["initialdir"] = str(self.last_input_dir)
        selected = filedialog.askopenfilenames(**options)
        if not selected:
            return
        self.add_files([Path(name) for name in selected])

    def scan(self, ffmpeg: str, videos: list[Path]) -> None:
        """Проверить ролики в рабочем потоке и отправить результаты в UI.

        Ошибка одного файла не прерывает проверку остальных; виджеты Tk здесь
        не трогаются, их обновляет обработчик событий главного окна.
        """
        for index, path in enumerate(videos, 1):
            if self.stopping.is_set():
                return
            try:
                info = probe_media(ffmpeg, path, stopping=self.stopping,
                                   track_process=self.worker.track_process)
                duration, rate = info.duration, info.frame_rate
            except (OSError, subprocess.TimeoutExpired):
                duration = None
                rate = None
                info = MediaInfo()
            if self.stopping.is_set():
                return
            self.events.put(("row", (index, len(videos), path, duration, rate, info)))
        if not self.stopping.is_set():
            self.events.put(("scan_done", None))

    def add_scanned_row(self, index: int, total: int, path: Path, duration: float | None,
                        frame_rate: str | None = None, info: MediaInfo | None = None) -> None:
        """Добавить найденную строку в модель и таблицу из потока Tk."""
        self.items.append((path, duration))
        self.video_frame_rates[path] = frame_rate
        if info is not None:
            self.video_metadata[path] = info
        self.empty_hint.place_forget()
        row = self.tree.insert("", "end", values=("", len(self.items), path.name,
                                format_duration(duration) if duration is not None else "Ошибка чтения", ""),
                                tags=(("stripe", "arrive") if len(self.items) % 2 == 0 else ("arrive",)))
        self.included_rows.add(row)
        self.update_include_heading()
        self.schedule_icon_refresh()
        # Краткий цветовой переход помогает заметить каждую поступившую строку.
        self.schedule_row_arrival(row)
        self.progress.configure(value=index)
        self.status.set(f"Чтение длительности: {index} из {total}")

    def finish_row_arrival(self, row: str) -> None:
        """Вернуть цвет строки после подсветки появления."""
        self.arrival_jobs.pop(row, None)
        if self.tree.exists(row):
            self.tree.item(row, tags=tuple(tag for tag in self.tree.item(row, "tags") if tag != "arrive"))
            self.schedule_icon_refresh()

    def schedule_row_arrival(self, row: str) -> None:
        """Запланировать снятие подсветки, отменяя повторный таймер строки."""
        if row in self.arrival_jobs:
            self.after_cancel(self.arrival_jobs[row])
        self.arrival_jobs[row] = self.after(350, self.finish_row_arrival, row)

    def cancel_row_arrivals(self) -> None:
        """Отменить таймеры строк при закрытии окна."""
        for job in self.arrival_jobs.values():
            self.after_cancel(job)
        self.arrival_jobs.clear()

    def finish_scan(self) -> None:
        """Разблокировать настройки после чтения списка роликов."""
        self.set_busy(False)
        self.update_videos()
        self.status.set(f"Готово к склейке: {len(self.videos)} видео (по порядку в таблице)")
