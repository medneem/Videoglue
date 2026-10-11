"""Ключевые сценарии интерфейса, настроек и склейки FFmpeg."""

import json
import math
import queue
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ffmpeg as media_ffmpeg
import merge
import settings
import video_list
from debug_log import DebugJournal
from window import VideoGlue


class VideoListTests(unittest.TestCase):
    def test_clear_estimate_and_progress_follow_checked_files(self) -> None:
        """Размер не требует запуска FFmpeg; очистка не удаляет исходники."""
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(settings, "config_path", return_value=Path(directory) / "config.jsonc"):
            app = VideoGlue()
            try:
                sources = [Path(directory) / name for name in ("a.mp4", "b.mp4")]
                for index, path in enumerate(sources, 1):
                    path.write_bytes(b"source")
                    info = media_ffmpeg.MediaInfo(10, "60", 20_000_000, True, 128000, 25_000_000, 1920, 1080)
                    app.add_scanned_row(index, 2, path, 10, "60", info)
                app.finish_scan()
                app.update()
                with patch("ffmpeg.subprocess.Popen") as launch:
                    app.quality_var.set("Малое")
                    app.refresh_estimate()
                    self.assertIn("12.3", app.estimated_size_var.get())
                    app.toggle_row(app.tree.get_children()[1])
                    app.compress_var.set(False)
                    app.refresh_estimate()
                    self.assertIn("23.8", app.estimated_size_var.get())
                    launch.assert_not_called()
                app.prepare_progress(len(app.videos))
                self.assertEqual(app.progress["value"], 0)
                app.toggle_all_rows()
                app.prepare_progress(len(app.videos))
                app.events.put(("stage_progress", ("Перекодирование", 2, 2, 20, 60, "b.mp4")))
                app.poll_events()
                self.assertEqual(app.progress["value"], 60)
                self.assertIn("Всего 60%", app.progress_label_var.get())
                app.set_busy(True)
                app.clear_files()
                self.assertEqual(len(app.items), 2)
                app.set_busy(False)
                app.clear_icon.event_generate("<Button-1>")
                app.refresh_estimate()
                self.assertFalse(app.tree.get_children())
                self.assertFalse(app.video_metadata)
                self.assertFalse(app.arrival_jobs)
                self.assertFalse(app.removal_jobs)
                self.assertTrue(all(path.exists() for path in sources))
            finally:
                app.close()

    def test_theme_switch_keeps_drop_zone_and_saves_preference(self) -> None:
        """Два оформления меняются без пересоздания окна или списка."""
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(settings, "config_path", return_value=Path(directory) / "config.jsonc"):
            app = VideoGlue()
            try:
                self.assertEqual(app.empty_hint.winfo_manager(), "place")
                app.theme_var.set("Тёмная")
                self.assertEqual(app.colors["panel"], "#202a38")
                self.assertEqual(app.empty_hint.cget("background"), app.colors["panel"])
                self.assertTrue(app.tree.winfo_exists())
                app.persist_settings()
                self.assertEqual(settings.load_settings().theme, "Тёмная")
                app.theme_var.set("Светлая")
                self.assertEqual(app.colors["panel"], "#ffffff")
            finally:
                app.close()

    def test_large_list_reuses_visible_icons_and_cancels_removal_on_close(self) -> None:
        """Длинный список не создаёт Canvas для каждого просмотренного файла."""
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(settings, "config_path", return_value=Path(directory) / "config.jsonc"):
            app = VideoGlue()
            try:
                for index in range(1000):
                    app.add_scanned_row(index + 1, 1000, Path(directory) / f"clip-{index}.mp4", 1)
                app.finish_scan()
                app.update()
                initial = sum(isinstance(child, video_list.tk.Canvas) for child in app.tree.winfo_children())
                for fraction in (0.1, 0.3, 0.6, 0.9, 1.0, 0.0):
                    app.tree.yview_moveto(fraction)
                    app.update()
                actual = sum(isinstance(child, video_list.tk.Canvas) for child in app.tree.winfo_children())
                self.assertLessEqual(actual, initial + 4)
                row = next(iter(app.row_icons))
                icon = app.row_icons[row][0]
                drawn = icon.find_all()
                app.refresh_row_icons()
                self.assertEqual(icon.find_all(), drawn)
                icon.event_generate("<Button-1>")
                self.assertNotIn(row, app.included_rows)
                self.assertEqual(len(app.videos), 999)
                app.remove_pressed_row = row
                with patch.object(app.tree, "identify_column", return_value="#6"), \
                        patch.object(app.tree, "identify_row", return_value=row):
                    app.finish_row_drag(SimpleNamespace(x=10, y=30))
                pending = list(app.removal_jobs[row])
                with patch.object(app, "after_cancel", wraps=app.after_cancel) as cancel:
                    app.close()
                    for job in pending:
                        self.assertIn(((job,), {}), cancel.call_args_list)
                self.assertFalse(app.tk.call("after", "info"))
            finally:
                if not app.stopping.is_set():
                    app.close()

    def test_saved_frame_rate_survives_scanning_and_selection(self) -> None:
        """Пустой список и сканирование не стирают сохранённый режим Авто."""
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(settings, "config_path", return_value=Path(directory) / "config.jsonc"):
            settings.save_settings(settings.AppSettings(frame_rate="Авто"))
            app = VideoGlue()
            try:
                self.assertEqual(app.frame_rate_box.cget("values"), ("Оригинал", "Авто"))
                app.persist_settings()
                self.assertEqual(settings.load_settings().frame_rate, "Авто")
                app.add_scanned_row(1, 1, Path(directory) / "video.mp4", 2, "60")
                app.finish_scan()
                self.assertEqual(app.frame_rate_var.get(), "Авто")
                app.toggle_all_rows()
                self.assertEqual(app.frame_rate_var.get(), "Авто")
                app.persist_settings()
                self.assertEqual(settings.load_settings().frame_rate, "Авто")
            finally:
                app.close()

    def test_encoding_controls_follow_compression_and_preserve_preferences(self) -> None:
        """Сжатие и занятость блокируют настройки, но не стирают выбор."""
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(settings, "config_path", return_value=Path(directory) / "config.jsonc"):
            app = VideoGlue()
            try:
                self.assertTrue(app.compress_var.get())
                self.assertEqual(app.quality_var.get(), "Максимальное")
                self.assertEqual(app.quality_box.cget("values"), tuple(merge.QUALITY_BITRATE))
                self.assertIn("Малое", app.quality_box.cget("values"))
                self.assertEqual(app.audio_quality_var.get(), "Оригинал")
                self.assertEqual(str(app.audio_quality_box.cget("state")), "readonly")
                self.assertEqual(str(app.frame_rate_box.cget("state")), "readonly")
                self.assertFalse(app.crop_black_border_var.get())
                self.assertFalse(app.remove_reflection_var.get())
                self.assertEqual(str(app.remove_reflection_check.cget("state")), "disabled")
                app.stabilization_var.set("Слабый")
                self.assertEqual(str(app.remove_reflection_check.cget("state")), "normal")
                app.crop_black_border_var.set(True)
                app.remove_reflection_var.set(True)
                app.audio_quality_var.set("AAC 256 кбит/с")
                app.compress_var.set(False)
                self.assertEqual(str(app.audio_quality_box.cget("state")), "disabled")
                self.assertEqual(str(app.frame_rate_box.cget("state")), "disabled")
                app.persist_settings()
                self.assertEqual(settings.load_settings().audio_quality, "AAC 256 кбит/с")
                self.assertFalse(settings.load_settings().compress)
                self.assertTrue(settings.load_settings().crop_black_border)
                self.assertTrue(settings.load_settings().remove_stabilization_reflection)
                self.assertEqual(str(app.crop_border_check.cget("state")), "disabled")
                self.assertEqual(str(app.remove_reflection_check.cget("state")), "disabled")
                app.compress_var.set(True)
                self.assertEqual(app.audio_quality_var.get(), "AAC 256 кбит/с")
                app.set_busy(True)
                self.assertEqual(str(app.crop_border_check.cget("state")), "disabled")
                self.assertEqual(str(app.remove_reflection_check.cget("state")), "disabled")
                self.assertEqual(str(app.audio_quality_box.cget("state")), "disabled")
                self.assertEqual(str(app.frame_rate_box.cget("state")), "disabled")
                app.set_busy(False)
                self.assertEqual(str(app.audio_quality_box.cget("state")), "readonly")
                self.assertEqual(str(app.frame_rate_box.cget("state")), "readonly")
            finally:
                app.close()

    def test_icons_follow_scrolling_and_cross_removes_row(self) -> None:
        """Накладные значки остаются на строках и выполняют удаление."""
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(settings, "config_path", return_value=Path(directory) / "config.jsonc"):
            app = VideoGlue()
            try:
                for index in range(1, 41):
                    app.add_scanned_row(index, 40, Path(directory) / f"clip-{index}.mp4", 1.0)
                app.finish_scan()
                app.update()
                first, last = app.tree.get_children()[0], app.tree.get_children()[-1]
                self.assertEqual(app.row_icons[first][0].winfo_manager(), "place")
                app.tree.yview_moveto(1.0)
                app.update()
                self.assertNotIn(first, app.row_icons)
                cross = app.row_icons[last][1]
                self.assertEqual(cross.winfo_manager(), "place")
                with patch.object(app.tree, "winfo_pointerx", return_value=app.tree.winfo_rootx() + cross.winfo_x() + 10), \
                        patch.object(app.tree, "winfo_pointery", return_value=app.tree.winfo_rooty() + cross.winfo_y() + 10):
                    cross.event_generate("<ButtonPress-1>", x=10, y=10)
                    cross.event_generate("<ButtonRelease-1>", x=10, y=10)
                deadline = time.monotonic() + 2
                while app.tree.exists(last) and time.monotonic() < deadline:
                    app.update()
                    time.sleep(0.01)
                self.assertFalse(app.tree.exists(last))
                self.assertEqual(len(app.items), 39)
            finally:
                app.close()

    def test_choose_files_adds_single_and_multiple_without_duplicates(self) -> None:
        """Повторный выбор дополняет список и сохраняет отметки предыдущих файлов."""
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(settings, "config_path", return_value=Path(directory) / "config.jsonc"):
            app = VideoGlue()
            try:
                folder = Path(directory)
                first = folder / "first clip.mp4"
                second = folder / "second.mkv"
                third = folder / "third.mov"
                unsupported = folder / "notes.txt"
                for path in (first, second, third, unsupported):
                    path.touch()
                self.assertEqual(app.choose_button.cget("text"), "＋  Добавить видео")
                with patch.object(video_list, "probe_media", return_value=media_ffmpeg.MediaInfo(duration=3.0)), \
                        patch.object(video_list.filedialog, "askopenfilenames",
                                     side_effect=[(str(first),), (str(first), str(second), str(unsupported), str(third)),
                                                  (str(first), str(second)), ()]) as dialog:
                    for _ in range(4):
                        app.choose_files()
                        deadline = time.monotonic() + 3
                        while app.busy and time.monotonic() < deadline:
                            app.poll_events()
                            time.sleep(0.01)
                        self.assertFalse(app.busy)
                        if len(app.items) == 1:
                            app.toggle_row(app.tree.get_children()[0])
                    self.assertEqual(dialog.call_count, 4)
                    self.assertEqual(dialog.call_args.kwargs["initialdir"], str(folder))
                self.assertEqual([path for path, _duration in app.items], [first, second, third])
                self.assertEqual(app.videos, [second, third])
                self.assertEqual(app.selected_duration.get(), "Общий хронометраж выбранных видео: 00:00:06")
                self.assertNotIn(app.tree.get_children()[0], app.included_rows)
                self.assertEqual(app.folder_label.cget("text"), str(folder))
                app.busy = True
                with patch.object(video_list.filedialog, "askopenfilenames") as dialog:
                    app.choose_files()
                    dialog.assert_not_called()
            finally:
                app.close()

    def test_drop_multiple_paths_with_spaces_and_reorder(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(settings, "config_path", return_value=Path(directory) / "config.jsonc"):
            app = VideoGlue()
            try:
                folder = Path(directory)
                first = folder / "first clip.mp4"
                second = folder / "second clip.mkv"
                broken = folder / "broken.avi"
                ignored = folder / "notes.txt"
                for path in (first, second, broken, ignored):
                    path.touch()

                data = app.tk.call("list", *(str(path) for path in
                                            (first, second, broken, ignored, first)))
                with patch.object(video_list, "probe_media", side_effect=lambda _ffmpeg, path, **_kwargs:
                                  media_ffmpeg.MediaInfo(duration=None if path == broken else 2.0)):
                    self.assertEqual(app.drop_files(SimpleNamespace(data=data)), video_list.COPY)
                    deadline = time.monotonic() + 3
                    while app.busy and time.monotonic() < deadline:
                        app.poll_events()
                        time.sleep(0.01)

                self.assertFalse(app.busy)
                rows = app.tree.get_children()
                self.assertEqual(len(rows), 3)
                self.assertEqual(app.videos, [first, second])
                self.assertEqual(app.selected_duration.get(), "Общий хронометраж выбранных видео: 00:00:04")
                self.assertEqual(app.tree.set(rows[2], "duration"), "Ошибка чтения")
                app.update()
                # В новой теме заголовок выше: координату берём внутри строки.
                row_y = app.tree.bbox(rows[0], "name")[1] + 5
                with patch.object(app.tree, "identify_column", return_value="#1"), \
                        patch.object(app.tree, "identify_row", return_value=rows[0]):
                    app.start_row_drag(SimpleNamespace(x=20, y=row_y))
                self.assertEqual(app.videos, [second])
                self.assertEqual(app.selected_duration.get(), "Общий хронометраж выбранных видео: 00:00:02")
                self.assertNotIn(rows[0], app.included_rows)
                app.heading_icon.event_generate("<Button-1>")
                self.assertEqual(app.videos, [first, second])
                app.toggle_all_rows()
                self.assertEqual(app.videos, [])
                self.assertEqual(app.selected_duration.get(), "Общий хронометраж выбранных видео: 00:00:00")
                self.assertEqual(str(app.merge_button.cget("state")), "disabled")
                app.toggle_all_rows()
                self.assertEqual(app.videos, [first, second])
                app.busy = True
                app.toggle_row(rows[0])
                app.toggle_all_rows()
                self.assertEqual(app.videos, [first, second])
                app.busy = False
                app.move_row(rows[1], rows[0])
                self.assertEqual(app.videos, [second, first])
                app.update()
                app.row_icons[rows[0]][0].event_generate("<Button-1>")
                self.assertEqual(app.videos, [second])
                app.toggle_row(rows[0])
                self.assertEqual(app.videos, [second, first])
                self.assertEqual(app.enter_file_drop(SimpleNamespace()), video_list.COPY)
                self.assertIsNotNone(app.drop_pulse_job)
                app.leave_file_drop()
                self.assertIsNone(app.drop_pulse_job)
                app.dragged_row = rows[0]
                app.drag_start_y = 0
                with patch.object(app.tree, "identify_row", return_value=rows[1]):
                    app.show_row_drag(SimpleNamespace(x=120, y=15))
                self.assertEqual(app.drag_preview.winfo_manager(), "place")
                self.assertIn("drag_source", app.tree.item(rows[0], "tags"))
                self.assertIn("drop_target", app.tree.item(rows[1], "tags"))
                gap = app.drag_gap
                self.assertIsNotNone(gap)
                self.assertEqual(app.tree.get_children(), (gap, rows[1], rows[0], rows[2]))
                with patch.object(app.tree, "identify_row", return_value=gap):
                    app.show_row_drag(SimpleNamespace(x=120, y=16))
                    self.assertEqual(app.drag_gap, gap)
                    app.finish_row_drag(SimpleNamespace(y=15))
                self.assertFalse(app.drag_preview.winfo_manager())
                self.assertNotIn("drag_source", app.tree.item(rows[0], "tags"))
                self.assertIsNone(app.drag_gap)
                self.assertEqual(app.tree.get_children(), (rows[0], rows[1], rows[2]))
                self.assertNotIn("drop_target", app.tree.item(rows[1], "tags"))
                app.dragged_row = rows[0]
                app.drag_start_y = 0
                with patch.object(app.tree, "identify_row", return_value=rows[2]):
                    app.show_row_drag(SimpleNamespace(x=120, y=15))
                gap = app.drag_gap
                self.assertEqual(app.tree.get_children(), (rows[0], rows[1], rows[2], gap))
                with patch.object(app.tree, "identify_row", return_value=gap):
                    app.finish_row_drag(SimpleNamespace(y=15))
                self.assertEqual(app.tree.get_children(), (rows[1], rows[2], rows[0]))
                self.assertEqual(app.videos, [second, first])
                with patch.object(app.tree, "identify_column", return_value="#3") as column, \
                        patch.object(app.tree, "identify_row", return_value=rows[0]), \
                        patch.object(video_list.os, "startfile", create=True) as open_file:
                    app.open_video(SimpleNamespace(x=100, y=30))
                    open_file.assert_called_once_with(first)
                    column.return_value = "#2"
                    app.open_video(SimpleNamespace(x=100, y=30))
                    open_file.assert_called_once_with(first)
                self.assertEqual(app.drop_files(SimpleNamespace(
                    data=app.tk.call("list", str(first)))), "none")
                self.assertEqual(app.tree.set(rows[2], "remove"), "")
                with patch.object(app.tree, "identify_column", return_value="#6") as column, \
                        patch.object(app.tree, "identify_row", return_value=rows[2]):
                    app.start_row_drag(SimpleNamespace(x=700, y=30))
                    column.return_value = "#3"
                    app.finish_row_drag(SimpleNamespace(x=100, y=30))
                    self.assertTrue(app.tree.exists(rows[2]))
                app.remove_row(rows[2])
                self.assertFalse(app.tree.exists(rows[2]))
                self.assertEqual(app.items, [(second, 2.0), (first, 2.0)])
                self.assertEqual(app.videos, [second, first])
                self.assertEqual([app.tree.set(row, "number") for row in app.tree.get_children()], [1, 2])
                app.busy = True
                app.remove_row(rows[1])
                self.assertTrue(app.tree.exists(rows[1]))
                app.busy = False
                app.remove_row(rows[1])
                app.remove_row(rows[0])
                self.assertEqual(app.items, [])
                self.assertEqual(app.videos, [])
                self.assertEqual(app.empty_hint.winfo_manager(), "place")
                self.assertEqual(app.selected_duration.get(), "Общий хронометраж выбранных видео: 00:00:00")
            finally:
                app.close()

    def test_output_path_opens_folder_on_click_and_only_after_success(self) -> None:
        """Показывать точное имя результата и открывать его каталог после успеха."""
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(settings, "config_path", return_value=Path(directory) / "config.jsonc"):
            app = VideoGlue()
            try:
                source = Path(directory) / "source.mp4"
                source.touch()
                app.items = [(source, 5.0)]
                app.videos = [source]
                app.gpu_checked = True
                chosen = Path(directory) / "merged.mov"
                expected = chosen.with_suffix(".mp4").resolve()
                with patch("window.filedialog.asksaveasfilename", return_value=str(chosen)), \
                        patch("window.ffmpeg_path", return_value="ffmpeg"), \
                        patch("window.threading.Thread") as thread:
                    app.merge()
                    thread.return_value.start.assert_called_once()
                self.assertEqual(app.output_path, expected)
                self.assertEqual(app.output_path_var.get(), f"Итоговый файл: {expected.name}")
                with patch("window.os.startfile", create=True) as open_folder, \
                        patch("window.sys.platform", "win32"), \
                        patch("window.messagebox.showinfo"):
                    app.output_button.invoke()
                    app.update()
                    open_folder.assert_called_once_with(expected.parent)
                    open_folder.reset_mock()
                    app.events.put(("merge_error", "failed"))
                    with patch("window.messagebox.showerror"):
                        app.poll_events()
                    open_folder.assert_not_called()
                    app.events.put(("merge_done", expected))
                    app.poll_events()
                    open_folder.assert_called_once_with(expected.parent)
            finally:
                app.close()

    def test_gpu_failure_requires_gui_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(settings, "config_path", return_value=Path(directory) / "config.jsonc"):
            app = VideoGlue()
            try:
                for answer in (False, True):
                    with self.subTest(answer=answer), \
                            patch("window.messagebox.askyesno", return_value=answer) as dialog:
                        result: list[bool] = []
                        thread = threading.Thread(target=lambda: result.append(
                            app.worker.request_cpu_fallback("Сбой OpenCL")))
                        thread.start()
                        deadline = time.monotonic() + 3
                        while not result and time.monotonic() < deadline:
                            app.poll_events()
                            time.sleep(0.01)
                        thread.join(timeout=1)
                        self.assertEqual(result, [answer])
                        dialog.assert_called_once()
                        self.assertNotIn("Сбой OpenCL", dialog.call_args.args[1])
            finally:
                app.close()


class JournalTests(unittest.TestCase):
    def test_rotation_and_unwritable_path_do_not_interrupt_processing(self) -> None:
        """Журнал ограничен по размеру, а отказ диска не подменяет исходную ошибку."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Videoglue.log"
            log = DebugJournal(path)
            try:
                log.write("x" * (6 * 1024 * 1024))
                log.command(["ffmpeg.exe", "-i", "C:\\видео с пробелом.mp4"])
                self.assertTrue(path.with_name("Videoglue.log.1").exists())
                self.assertLessEqual(path.stat().st_size, 5 * 1024 * 1024)
                self.assertIn("видео с пробелом", path.read_text(encoding="utf-8"))
                with patch.object(log.handler.stream, "write", side_effect=OSError("disk full")):
                    log.write("failed write")
                self.assertTrue(log.failed)
            finally:
                log.close()
            failed = DebugJournal(Path(directory) / "missing" / "Videoglue.log")
            failed.write("diagnostic")
            self.assertTrue(failed.failed)
            self.assertIn("Не удалось", failed.notice())


class SettingsTests(unittest.TestCase):
    def test_jsonc_round_trip_and_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.jsonc"
            path.write_text('''// Настройки
{
  "input_dir": "C:\\\\videos//name", /* путь со слешами */
  "format": "MKV", "compress": true, "use_gpu": false,
  "deinterlace": "Принудительно", "stabilization": "Сильный", "frame_rate": "Авто",
   "audio_quality": "AAC 128 кбит/с", "resolution": "720p",
  "unknown": ["/*literal*/",],
}
''', encoding="utf-8")
            loaded = settings.load_settings(path)
            self.assertEqual(loaded.input_dir, "C:\\videos//name")
            self.assertEqual((loaded.format, loaded.compress, loaded.use_gpu), ("MKV", True, False))
            self.assertEqual((loaded.deinterlace, loaded.stabilization), ("Принудительно", "Сильный"))
            self.assertEqual(loaded.frame_rate, "Авто")
            self.assertEqual(loaded.audio_quality, "AAC 128 кбит/с")
            settings.save_settings(loaded, path)
            self.assertEqual(json.loads(settings.strip_jsonc(path.read_text(encoding="utf-8")))["format"], "MKV")

            loaded.quality = "Малое"
            loaded.theme = "Тёмная"
            settings.save_settings(loaded, path)
            self.assertEqual(settings.load_settings(path).quality, "Малое")
            self.assertEqual(settings.load_settings(path).theme, "Тёмная")

            path.write_text('{"width": 1, "compress": "yes", "stabilization": "bad", "frame_rate": "100"}', encoding="utf-8")
            self.assertEqual(settings.load_settings(path), settings.AppSettings())
            path.write_text('{"compress":', encoding="utf-8")
            self.assertEqual(settings.load_settings(path), settings.AppSettings())

    def test_old_config_enables_compression_once(self) -> None:
        """Миграция включает сжатие старым пользователям только один раз."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.jsonc"
            path.write_text('{"compress": false, "quality": "Высокое", "frame_rate": "23,976"}',
                            encoding="utf-8")
            migrated = settings.load_settings(path)
            self.assertTrue(migrated.compress)
            self.assertEqual(migrated.quality, "Высокое")
            self.assertEqual(migrated.frame_rate, "Оригинал")
            migrated.compress = False
            settings.save_settings(migrated, path)
            self.assertFalse(settings.load_settings(path).compress)


class MetadataTests(unittest.TestCase):
    def test_metadata_read_once_and_invalidated_after_file_change(self) -> None:
        """Длительность, FPS и битрейт используют один процесс, но не устаревший файл."""
        ffmpeg = media_ffmpeg.ffmpeg_path()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=s=320x240:r=60", "-t", "1", "-c:v", "mpeg4", "-y", str(source)],
                           check=True, capture_output=True, creationflags=media_ffmpeg.NO_WINDOW)
            with patch.object(media_ffmpeg.subprocess, "Popen", wraps=subprocess.Popen) as launch:
                duration = media_ffmpeg.probe_duration(ffmpeg, source)
                self.assertEqual(media_ffmpeg.probe_frame_rate(ffmpeg, source), "60")
                self.assertGreater(media_ffmpeg.probe_video_bitrate(ffmpeg, source, duration), 0)
                self.assertEqual(launch.call_count, 1)
                with source.open("ab") as stream:
                    stream.write(b"\n")
                self.assertEqual(media_ffmpeg.probe_duration(ffmpeg, source), duration)
                self.assertEqual(launch.call_count, 2)

    def test_short_diagnostic_timeout_kills_and_reaps_child(self) -> None:
        """После тайм-аута не остаются дочерний процесс и открытые каналы."""
        tracked = []
        with self.assertRaises(subprocess.TimeoutExpired):
            media_ffmpeg.run_diagnostic([sys.executable, "-c", "import time; time.sleep(60)"],
                                       0.05, track_process=tracked.append)
        child, cleared = tracked
        self.assertIsNone(cleared)
        self.assertIsNotNone(child.poll())
        self.assertTrue(child.stderr.closed)

    def test_small_bitrate_fallback_reserves_actual_audio(self) -> None:
        """Небольшое видео со звуком не теряет весь бюджет из-за резерва 512 кбит/с."""
        info = media_ffmpeg.MediaInfo(has_audio=True, audio_bitrate=64_000, file_size=32_000)
        self.assertEqual(info.estimated_video_bitrate(Path("small.mkv"), 1), 192_000)
        unknown = media_ffmpeg.MediaInfo(has_audio=True, file_size=32_000)
        self.assertGreater(unknown.estimated_video_bitrate(Path("small.mkv"), 1), 0)


class MergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: queue.SimpleQueue[tuple[str, object]] = queue.SimpleQueue()
        self.worker = merge.MergeWorker(self.events, threading.Event())
        self.ffmpeg = media_ffmpeg.ffmpeg_path()

    def drain_events(self) -> list[tuple[str, object]]:
        events = []
        while not self.events.empty():
            events.append(self.events.get())
        return events

    def test_progress_uses_clip_position_and_weighted_stage_time(self) -> None:
        """Переход ролика сбрасывает локальный процент, но не общий."""
        self.assertEqual(merge.progress_position(5, [10, 30]), (1, 50, 12))
        self.assertEqual(merge.progress_position(10, [10, 30]), (2, 0, 25))
        self.assertEqual(merge.progress_position(25, [10, 30]), (2, 50, 62))
        self.assertEqual(merge.progress_position(45, [10, 30]), (2, 99, 99))

    def test_mixed_dv_h264_and_silent_clip_normalize_video_and_audio(self) -> None:
        """Смена размера/кодека/звука не обрывает граф; тишина сохраняет хронометраж."""
        probe = shutil.which("ffprobe")
        if not probe:
            self.skipTest("FFprobe нужен только для проверки результата")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            dv, hd, silent = folder / "dv.avi", folder / "hd.mp4", folder / "silent.mp4"
            for path, size, rate, codec, audio in (
                    (dv, "720x576", 25, "dvvideo", "pcm_s16le"),
                    (hd, "640x360", 60, "libx264", "aac"),
                    (silent, "160x120", 30, "libx264", None)):
                command = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                           "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}"]
                if audio:
                    command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-c:a", audio]
                command += ["-t", "1", "-c:v", codec, "-pix_fmt", "yuv420p", str(path)]
                subprocess.run(command, check=True, capture_output=True, timeout=20)
            sources = [dv, hd, silent]
            modes = [(False, "Как у первого ролика", (720, 576)), (False, "720p", (1280, 720))]
            if media_ffmpeg.nvenc_available(self.ffmpeg):
                modes.append((True, "1080p", (1920, 1080)))
            for gpu, resolution, dimensions in modes:
                with self.subTest(gpu=gpu, resolution=resolution), \
                        patch.object(self.worker, "request_cpu_fallback", return_value=False):
                    output = folder / f"out-{gpu}-{dimensions[0]}.mp4"
                    self.worker.run_merge(self.ffmpeg, sources, output, compress=True,
                                          use_gpu=gpu, deinterlace="Принудительно", stabilization="Слабый",
                                          resolution=resolution)
                    events = self.drain_events()
                    self.assertEqual(events[-1], ("merge_done", output), events)
                    data = json.loads(subprocess.run([probe, "-v", "error", "-count_frames", "-show_streams",
                                                      "-of", "json", str(output)], check=True,
                                                     capture_output=True, timeout=20).stdout)
                    video = next(item for item in data["streams"] if item["codec_type"] == "video")
                    audio = next(item for item in data["streams"] if item["codec_type"] == "audio")
                    self.assertEqual((video["width"], video["height"]), dimensions)
                    self.assertEqual(int(video["nb_read_frames"]), 230)  # Принудительно удвоены все входы.
                    self.assertEqual(audio["codec_name"], "aac")
                    self.assertEqual(audio["sample_rate"], "48000")
                    self.assertAlmostEqual(float(video["duration"]), 3, delta=0.1)
                    self.assertAlmostEqual(float(audio["duration"]), 3, delta=0.1)
                    if not gpu:
                        analysis = [event[1] for event in events if event[0] == "stage_progress"
                                    and event[1][0] == "Анализ стабилизации"]
                        self.assertTrue(analysis)
                        self.assertEqual([item[4] for item in analysis], sorted(item[4] for item in analysis))

    def test_metadata_timeout_reports_error_and_preserves_existing_output(self) -> None:
        """Ошибка подготовки приходит в GUI, а прежний итоговый файл не изменяется."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.mkv"
            output.write_bytes(b"previous result")
            with patch.object(merge, "probe_media", side_effect=subprocess.TimeoutExpired("ffmpeg", 30)):
                self.worker.run_merge(self.ffmpeg, [Path(directory) / "source.mkv"], output,
                                      compress=True, use_gpu=False)
            events = self.drain_events()
            self.assertEqual(events[-1][0], "merge_error")
            self.assertIn("30 секунд", events[-1][1])
            self.assertEqual(output.read_bytes(), b"previous result")

    def test_stop_during_metadata_read_terminates_worker_and_child(self) -> None:
        """Закрытие прерывает подготовку, не ожидая 30-секундного тайм-аута."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source, output = folder / "source.mp4", folder / "result.mkv"
            source.touch()
            started = threading.Event()
            children = []
            original_launch = subprocess.Popen

            def launch(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
                child = original_launch([sys.executable, "-c", "import time; time.sleep(60)"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                        creationflags=media_ffmpeg.NO_WINDOW)
                children.append(child)
                return child

            original_track = self.worker.track_process

            def track(child: subprocess.Popen[bytes] | None) -> None:
                original_track(child)
                if child is not None:
                    started.set()

            with patch.object(media_ffmpeg.subprocess, "Popen", side_effect=launch), \
                    patch.object(self.worker, "track_process", side_effect=track):
                thread = threading.Thread(target=self.worker.run_merge,
                                          args=(self.ffmpeg, [source], output),
                                          kwargs={"compress": True, "use_gpu": False})
                thread.start()
                try:
                    self.assertTrue(started.wait(3))
                    self.worker.stop()
                    thread.join(timeout=3)
                    self.assertFalse(thread.is_alive())
                    self.assertIsNone(self.worker.process)
                    self.assertIsNotNone(children[0].poll())
                    self.assertFalse(output.exists())
                    self.assertEqual(self.drain_events(), [])
                finally:
                    self.worker.stop()
                    thread.join(timeout=3)

    def make_video(self, path: Path, *, filter_graph: str = "null", rate: str = "25") -> None:
        subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             f"testsrc2=s=320x240:r={rate}", "-t", "2", "-vf", filter_graph,
             "-c:v", "mpeg4", "-an", "-y", str(path)],
            check=True, capture_output=True, creationflags=media_ffmpeg.NO_WINDOW,
        )

    def test_audio_quality_copies_or_encodes_requested_bitrate(self) -> None:
        """Оригинал сохраняет аудиопакеты, AAC создаёт выбранный поток."""
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            self.skipTest("FFprobe недоступен")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "source.mkv"
            subprocess.run(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "testsrc2=s=320x240:r=30", "-f", "lavfi", "-i",
                 "anoisesrc=color=white:sample_rate=48000", "-t", "2", "-c:v", "mpeg4",
                 "-c:a", "flac", "-y", str(source)],
                check=True, capture_output=True, creationflags=media_ffmpeg.NO_WINDOW)

            def audio_info(path: Path) -> dict[str, object]:
                """Прочитать кодек, битрейт и хеши звуковых пакетов."""
                result = subprocess.run(
                    [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_streams",
                     "-show_packets", "-show_data_hash", "sha256", "-of", "json", str(path)],
                    check=True, capture_output=True, text=True, creationflags=media_ffmpeg.NO_WINDOW)
                return json.loads(result.stdout)

            original = audio_info(source)
            copied = folder / "copied.mkv"
            self.worker.run_merge(self.ffmpeg, [source], copied, compress=False,
                                  audio_quality="AAC 256 кбит/с")
            self.assertEqual(self.drain_events()[-1], ("merge_done", copied))
            self.assertEqual([packet["data_hash"] for packet in audio_info(copied)["packets"]],
                             [packet["data_hash"] for packet in original["packets"]])
            for quality in settings.AUDIO_QUALITIES:
                with self.subTest(quality=quality):
                    suffix = ".mkv" if quality == "Оригинал" else ".mp4"
                    output = folder / f"result-{settings.AUDIO_QUALITIES.index(quality)}{suffix}"
                    gpu = quality == "AAC 192 кбит/с" and media_ffmpeg.nvenc_available(self.ffmpeg)
                    self.worker.run_merge(self.ffmpeg, [source], output, compress=True,
                                          use_gpu=gpu, deinterlace="Выключено", audio_quality=quality)
                    events = self.drain_events()
                    self.assertEqual(events[-1], ("merge_done", output), events)
                    self.assertEqual([data for kind, data in events if kind == "encoding"],
                                     ["nvenc" if gpu else "cpu"])
                    result = audio_info(output)
                    if quality == "Оригинал":
                        self.assertEqual(result["streams"][0]["codec_name"], "flac")
                        self.assertEqual([packet["data_hash"] for packet in result["packets"]],
                                         [packet["data_hash"] for packet in original["packets"]])
                    else:
                        self.assertEqual(result["streams"][0]["codec_name"], "aac")
                        target = int(quality.split()[1]) * 1000
                        self.assertAlmostEqual(int(result["streams"][0]["bit_rate"]), target,
                                               delta=target * 0.35)

            invalid = folder / "invalid.mkv"
            self.worker.run_merge(self.ffmpeg, [source], invalid, compress=True,
                                  audio_quality="Неизвестно")
            self.assertEqual(self.drain_events()[-1][0], "merge_error")
            self.assertFalse(invalid.exists())

    def test_copy_rejects_unsupported_container_and_preserves_codec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "dv.avi"
            subprocess.run(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "color=c=blue:s=720x480:r=30000/1001", "-t", "0.3",
                 "-c:v", "dvvideo", "-pix_fmt", "yuv411p", "-an", "-y", str(source)],
                check=True, capture_output=True, creationflags=media_ffmpeg.NO_WINDOW,
            )
            output = folder / "result.mp4"
            with patch.object(merge, "detect_field_order", side_effect=AssertionError("analysis")), \
                    patch.object(merge, "nvenc_available", side_effect=AssertionError("GPU")), \
                    patch.object(merge, "probe_media", side_effect=AssertionError("metadata")), \
                    patch.object(self.worker, "analyze_stabilization", side_effect=AssertionError("stabilization")):
                self.worker.run_merge(self.ffmpeg, [source], output,
                                      deinterlace="Принудительно", stabilization="Сильный", frame_rate="60")
                events = self.drain_events()
                self.assertEqual(events[-1][0], "merge_error")
                self.assertIn("Выберите MKV или включите «Сжать»", events[-1][1])
                self.assertFalse(output.exists())

                copied = folder / "copied.mkv"
                self.worker.run_merge(self.ffmpeg, [source], copied,
                                      deinterlace="Принудительно", stabilization="Сильный", frame_rate="60")
            self.assertEqual(self.drain_events()[-1], ("merge_done", copied))
            self.assertTrue(copied.is_file())
            self.assertIn("Video: dvvideo", subprocess.run(
                [self.ffmpeg, "-hide_banner", "-i", str(copied)], capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                creationflags=media_ffmpeg.NO_WINDOW,
            ).stderr)

    def test_auto_deinterlace_and_stabilization_on_mixed_clips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            interlaced = folder / "interlaced.mkv"
            progressive = folder / "progressive.mkv"
            self.make_video(interlaced, filter_graph="interlace=scan=bff,setfield=prog", rate="50")
            self.make_video(progressive)
            self.assertEqual(media_ffmpeg.detect_field_order(self.ffmpeg, interlaced, 2), "bff")
            self.assertIsNone(media_ffmpeg.detect_field_order(self.ffmpeg, progressive, 2))

            output = folder / "merged.mkv"
            self.worker.run_merge(self.ffmpeg, [interlaced, progressive], output,
                                  compress=True, use_gpu=False, durations=[2, 2],
                                  total_seconds=4, stabilization="Слабый")
            events = self.drain_events()
            self.assertEqual(events[-1], ("merge_done", output))
            self.assertEqual([data for kind, data in events if kind == "stabilizing"], [(1, 2), (2, 2)])
            self.assertEqual([data for kind, data in events if kind == "encoding"], ["cpu"])
            self.assertTrue(output.is_file())
            self.assertIsNone(media_ffmpeg.detect_field_order(self.ffmpeg, output, 4))

    def test_stabilization_preserves_sixty_fps_and_all_frames(self) -> None:
        """Склейка однородных роликов не должна превращать 60 кадров/с в 25."""
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            self.skipTest("FFprobe недоступен")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            videos = [folder / f"clip-{index}.mkv" for index in range(2)]
            for video in videos:
                self.make_video(video, rate="60")
            self.assertEqual(media_ffmpeg.probe_frame_rate(self.ffmpeg, videos[0]), "60")
            for gpu, clips in ((False, videos), (True, videos[:1]), (True, videos)):
                if gpu and not media_ffmpeg.nvenc_available(self.ffmpeg):
                    continue
                with self.subTest(gpu=gpu, clips=len(clips)):
                    output = folder / f"result-{gpu}-{len(clips)}.mkv"
                    with patch.object(self.worker, "request_cpu_fallback", return_value=False):
                        self.worker.run_merge(self.ffmpeg, clips, output,
                                              compress=True, use_gpu=gpu,
                                              deinterlace="Выключено", stabilization="Слабый",
                                              durations=[2] * len(clips), total_seconds=2 * len(clips))
                    events = self.drain_events()
                    self.assertEqual(events[-1], ("merge_done", output), events)
                    self.assertEqual([value for kind, value in events if kind == "encoding"],
                                     ["nvenc" if gpu else "cpu"])
                    info = subprocess.run(
                        [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
                         "-show_entries", "stream=avg_frame_rate,nb_read_frames",
                         "-of", "json", str(output)],
                        check=True, capture_output=True, text=True,
                        creationflags=media_ffmpeg.NO_WINDOW,
                    )
                    stream = json.loads(info.stdout)["streams"][0]
                    numerator, denominator = map(int, stream["avg_frame_rate"].split("/"))
                    self.assertEqual(int(stream["nb_read_frames"]), 120 * len(clips))
                    self.assertAlmostEqual(numerator / denominator, 60, delta=0.1)

    def test_stabilization_reduces_known_shake_after_buffer_fills(self) -> None:
        """Проверяем быструю/медленную тряску и неподвижный фон с движущимся объектом."""
        width, height, margin, count = 640, 360, 24, 180
        stride = width + 2 * margin
        rng = random.Random(481)
        tiles = [[rng.randrange(30, 225) for _ in range((stride + 7) // 8)]
                 for _ in range((height + 7) // 8)]
        texture = bytes(tiles[y // 8][x // 8] for y in range(height) for x in range(stride))
        reference = b"".join(texture[y * stride + margin:y * stride + margin + width]
                             for y in range(height))
        points = [y * width + x for y in range(44, height - 40, 24)
                  for x in range(41, width - 40, 13)]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)

            def run(args: list[str]) -> bytes:
                result = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args],
                                        cwd=folder, capture_output=True,
                                        creationflags=media_ffmpeg.NO_WINDOW)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                return result.stdout

            def measure(path: Path, sample_points: list[int]) -> list[int]:
                data = run(["-i", str(path), "-an", "-pix_fmt", "gray", "-fps_mode", "passthrough",
                            "-f", "rawvideo", "pipe:1"])
                self.assertEqual(len(data), width * height * count)
                positions = []
                for k in range(count):
                    frame = data[k * width * height:(k + 1) * width * height]
                    positions.append(min(range(-20, 21), key=lambda dx:
                                         sum((frame[i + dx] - reference[i]) ** 2 for i in sample_points)))
                return positions

            has_gpu = media_ffmpeg.nvenc_available(self.ffmpeg)
            for scenario in ("fast", "slow", "foreground", "pan"):
                raw, source = folder / "shaky.gray", folder / "shaky.mp4"
                with raw.open("wb") as stream:
                    for k in range(count):
                        shift = (round(4 * math.sin(k * 1.7)) if scenario == "fast" else
                                 round(4 * math.sin(k * 2 * math.pi / 90)) if scenario == "slow" else
                                 round(k * 0.07) if scenario == "pan" else 0)
                        frame = bytearray(b"".join(texture[y * stride + margin + shift:
                                                          y * stride + margin + shift + width]
                                                  for y in range(height)))
                        if scenario == "foreground":
                            # Движется большая центральная область, но не камера.
                            moving = round(12 * math.sin(k * 0.2))
                            for y in range(24, height - 24):
                                left, right = width // 5, width * 4 // 5
                                start = y * stride + margin + left + moving
                                frame[y * width + left:y * width + right] = texture[start:start + right-left]
                        stream.write(frame)
                run(["-f", "rawvideo", "-pixel_format", "gray", "-video_size", f"{width}x{height}",
                     "-framerate", "30", "-i", str(raw), "-vf", "format=yuv420p", "-c:v", "libx264",
                     "-crf", "10", str(source)])
                sample_points = ([p for p in points if p % width < width // 5 - 20 or
                                  p % width > width * 4 // 5 + 20] if scenario == "foreground" else points)
                before = measure(source, sample_points)
                run(["-i", str(source), "-vf", "vidstabdetect=result=stab-0.trf", "-an", "-f", "null", "-"])
                for gpu in (False, True):
                    if gpu and not has_gpu:
                        continue
                    with self.subTest(scenario=scenario, gpu=gpu):
                        output = folder / f"stabilized-{gpu}.mp4"
                        inputs = merge.filtered_inputs(
                            folder / "unused.txt", [source], [None], "Сильный",
                            gpu_stabilization=gpu, frame_rate="30",
                            metadata=[media_ffmpeg.MediaInfo(width=width, height=height)],
                            audio_infos=[media_ffmpeg.MediaInfo(has_audio=False)],
                            remove_reflection=not gpu)
                        run([*inputs, "-an", "-c:v", "h264_nvenc" if gpu else "libx264", str(output)])
                        after = measure(output, sample_points)
                        if scenario == "foreground":
                            self.assertLess(statistics.pstdev(after), 0.5, after)
                            self.assertLessEqual(max(after) - min(after), 1, after)
                        elif scenario == "pan":
                            self.assertAlmostEqual(after[-1] - after[0], before[-1] - before[0], delta=3)
                        else:
                            self.assertLess(statistics.pstdev(after), statistics.pstdev(before) * 0.75)
                            self.assertLess(statistics.mean(abs(a-b) for a,b in zip(after, after[1:])),
                                            statistics.mean(abs(a-b) for a,b in zip(before, before[1:])) * 0.75)

    def test_gpu_stabilization_keeps_both_deinterlaced_fields(self) -> None:
        """Восстановление FPS после OpenCL не выбрасывает половину полей bwdif."""
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None or not media_ffmpeg.nvenc_available(self.ffmpeg):
            self.skipTest("FFprobe или NVIDIA NVENC недоступны")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source, output = folder / "interlaced.mkv", folder / "result.mkv"
            self.make_video(source, filter_graph="interlace=scan=bff,setfield=prog", rate="50")
            with patch.object(self.worker, "request_cpu_fallback", return_value=False):
                self.worker.run_merge(self.ffmpeg, [source], output, compress=True, use_gpu=True,
                                      stabilization="Слабый", deinterlace="Авто")
            events = self.drain_events()
            self.assertEqual(events[-1], ("merge_done", output), events)
            result = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
                                     "-show_entries", "stream=avg_frame_rate,nb_read_frames", "-of", "json", str(output)],
                                    check=True, capture_output=True, text=True, creationflags=media_ffmpeg.NO_WINDOW)
            stream = json.loads(result.stdout)["streams"][0]
            self.assertEqual(int(stream["nb_read_frames"]), 100)
            numerator, denominator = map(int, stream["avg_frame_rate"].split("/"))
            self.assertAlmostEqual(numerator / denominator, 50, delta=0.1)

    def test_frame_rate_limits_output_without_raising_source_fps(self) -> None:
        """Авто снижает только высокую частоту каждого ролика отдельно."""
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            self.skipTest("FFprobe недоступен")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            fast, slow = folder / "fast.mkv", folder / "slow.mkv"
            fractional = folder / "fractional.mkv"
            self.make_video(fast, rate="60")
            self.make_video(slow, rate="30")
            self.make_video(fractional, rate="60000/1001")
            self.assertEqual(media_ffmpeg.probe_frame_rate(self.ffmpeg, fractional), "59.94")
            for gpu, stabilization, clips, expected, result_rate in (
                (False, "Выключен", [fast, slow], 120, 30),
                (False, "Выключен", [fast], 60, 30),
                (False, "Выключен", [fractional], 60, 29.97),
                (False, "Слабый", [fast], 60, 30),
                (True, "Слабый", [fast], 60, 30),
            ):
                if gpu and not media_ffmpeg.nvenc_available(self.ffmpeg):
                    continue
                with self.subTest(clips=len(clips), gpu=gpu, stabilization=stabilization):
                    output = folder / f"result-{gpu}-{stabilization}-{len(clips)}-{clips[0].stem}.mkv"
                    with patch.object(self.worker, "request_cpu_fallback", return_value=False):
                        self.worker.run_merge(self.ffmpeg, clips, output, compress=True, use_gpu=gpu,
                                              deinterlace="Выключено", stabilization=stabilization,
                                               frame_rate="Авто", durations=[2] * len(clips))
                    events = self.drain_events()
                    self.assertEqual(events[-1], ("merge_done", output), events)
                    info = subprocess.run(
                        [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
                         "-show_entries", "stream=avg_frame_rate,nb_read_frames", "-of", "json", str(output)],
                        check=True, capture_output=True, text=True, creationflags=media_ffmpeg.NO_WINDOW)
                    stream = json.loads(info.stdout)["streams"][0]
                    numerator, denominator = map(int, stream["avg_frame_rate"].split("/"))
                    self.assertEqual(int(stream["nb_read_frames"]), expected)
                    self.assertAlmostEqual(numerator / denominator, result_rate, delta=0.1)
            forbidden = folder / "forbidden.mkv"
            with patch.object(merge, "detect_field_order", side_effect=AssertionError("analysis")):
                self.worker.run_merge(self.ffmpeg, [slow], forbidden, compress=True,
                                       frame_rate="60", deinterlace="Авто")
            self.assertEqual(self.drain_events()[-1][0], "merge_error")
            self.assertFalse(forbidden.exists())

    def test_bitrate_budget_weights_video_durations_and_quality(self) -> None:
        """Бюджет учитывает длительность каждого ролика, а не число файлов."""
        files = [Path("fast.mp4"), Path("slow.mp4")]
        self.assertEqual(merge.QUALITY_BITRATE, {
            "Максимальное": 0.90, "Высокое": 0.70,
            "Компактное": 0.45, "Малое": 0.25,
        })
        with patch.object(merge, "probe_media", side_effect=[
                media_ffmpeg.MediaInfo(duration=100, video_bitrate=20_000_000),
                media_ffmpeg.MediaInfo(duration=200, video_bitrate=10_000_000)] * len(merge.QUALITY_BITRATE)):
            for quality, ratio in merge.QUALITY_BITRATE.items():
                with self.subTest(quality=quality):
                    target, peak = merge.video_bitrate_budget(
                        self.ffmpeg, files, quality)
                    self.assertEqual(target, int((20_000_000 * 100 + 10_000_000 * 200) * ratio / 300))
                    self.assertEqual(peak, min(13_333_333, int(target * 1.5)))

    def test_adaptive_budget_handles_dv_full_hd_and_output_frame_rate(self) -> None:
        """DV ограничивается пикселями, H.264 — долей источника; FPS учитывает фильтры."""
        dv = media_ffmpeg.MediaInfo(duration=100, frame_rate="25", video_bitrate=28_800_000,
                                    width=720, height=576)
        hd = media_ffmpeg.MediaInfo(duration=200, frame_rate="60", video_bitrate=21_293_000,
                                    width=1920, height=1080)
        paths = [Path("dv.avi"), Path("hd.mp4")]
        dv_target, dv_peak = merge.video_bitrate_budget(
            self.ffmpeg, paths[:1], "Малое", [dv], field_orders=["bff"])
        self.assertEqual(dv_target, 2_073_600)
        self.assertEqual(dv_peak, 3_110_400)
        self.assertEqual(merge.video_bitrate_budget(
            self.ffmpeg, paths[:1], "Малое", [dv], field_orders=["bff"],
            auto_rates=["25"])[0], 1_036_800)
        hd_target = merge.video_bitrate_budget(self.ffmpeg, paths[1:], "Малое", [hd])[0]
        self.assertEqual(hd_target, 5_323_250)
        self.assertEqual(merge.video_bitrate_budget(
            self.ffmpeg, paths[1:], "Максимальное", [hd], auto_rates=["30"])[0], 18_662_400)
        mixed = merge.video_bitrate_budget(
            self.ffmpeg, paths, "Малое", [dv, hd], field_orders=["bff", None])[0]
        self.assertEqual(mixed, int((dv_target * 100 + hd_target * 200) / 300))

    def test_cpu_preserves_first_frame_detail_after_concat(self) -> None:
        """Микросекундная шкала concat не должна разрушать первый кадр VBR."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source, output = folder / "source.mp4", folder / "result.mp4"
            subprocess.run(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "testsrc2=s=640x360:r=30", "-t", "2", "-c:v", "libx264", "-crf", "18",
                 "-y", str(source)], check=True, capture_output=True,
                creationflags=media_ffmpeg.NO_WINDOW)
            self.worker.run_merge(self.ffmpeg, [source], output, compress=True,
                                  quality="Максимальное", use_gpu=False, deinterlace="Выключено")
            self.assertEqual(self.drain_events()[-1], ("merge_done", output))

            def first_frame(path: Path) -> bytes:
                """Прочитать яркость первого декодированного кадра без внешних библиотек."""
                result = subprocess.run(
                    [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path),
                     "-frames:v", "1", "-an", "-c:v", "rawvideo", "-pix_fmt", "yuv420p", "-f", "avi", "-"],
                    check=True, capture_output=True, creationflags=media_ffmpeg.NO_WINDOW)
                # Комплектный FFmpeg поддерживает AVI, но не rawvideo muxer.
                start = result.stdout.index(b"movi") + 4
                self.assertIn(result.stdout[start:start + 4], (b"00dc", b"00db"))
                return result.stdout[start + 8:start + 8 + 640 * 360]

            original, encoded = first_frame(source), first_frame(output)
            self.assertEqual(len(encoded), 640 * 360)
            error = sum((left - right) ** 2 for left, right in zip(original, encoded, strict=True)) / len(original)
            self.assertLess(error, 100, f"Первый кадр потерял детали: MSE={error:.1f}")

    def test_original_keeps_variable_frame_timing_without_duplication(self) -> None:
        """Вход 60 fps с пропусками кадров нельзя расширять до ровных 60."""
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            self.skipTest("FFprobe недоступен")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source, output = folder / "variable.mp4", folder / "result.mp4"
            subprocess.run(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "testsrc2=s=320x240:r=60", "-t", "2", "-vf", "select=mod(n\\,10)",
                 "-fps_mode", "vfr", "-c:v", "libx264", "-y", str(source)],
                check=True, capture_output=True, creationflags=media_ffmpeg.NO_WINDOW)
            self.worker.run_merge(self.ffmpeg, [source], output, compress=True,
                                  use_gpu=False, deinterlace="Выключено", frame_rate="Оригинал")
            self.assertEqual(self.drain_events()[-1], ("merge_done", output))
            def frames(path: Path) -> int:
                result = subprocess.run(
                    [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
                     "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", str(path)],
                    check=True, capture_output=True, text=True, creationflags=media_ffmpeg.NO_WINDOW)
                return int(result.stdout.strip())
            self.assertEqual(frames(source), 108)
            self.assertEqual(frames(output), frames(source))

    def test_nvenc_variable_rate_respects_budget_and_frame_timestamps(self) -> None:
        """VFR после concat не удваивает бюджет NVENC и не меняет временные метки."""
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None or not media_ffmpeg.nvenc_available(self.ffmpeg):
            self.skipTest("FFprobe или NVIDIA NVENC недоступен")
        if not media_ffmpeg.nvenc_rate_hint_supported(self.ffmpeg):
            self.skipTest("Сборка FFmpeg без исправления rc_framerate")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source, output = folder / "variable.mp4", folder / "result.mp4"
            subprocess.run(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "testsrc2=s=1280x720:r=60", "-t", "8", "-vf", "select=mod(n\\,10)",
                 "-fps_mode", "vfr", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                 "-y", str(source)], check=True, capture_output=True,
                creationflags=media_ffmpeg.NO_WINDOW)
            info = media_ffmpeg.probe_media(self.ffmpeg, source)
            target, _ = merge.video_bitrate_budget(
                self.ffmpeg, [source, source], "Малое", [info, info])
            with patch.object(self.worker, "request_cpu_fallback", return_value=False):
                self.worker.run_merge(self.ffmpeg, [source, source], output, compress=True,
                                      quality="Малое", use_gpu=True, deinterlace="Выключено")
            events = self.drain_events()
            self.assertEqual(events[-1], ("merge_done", output), events)
            self.assertEqual([data for kind, data in events if kind == "encoding"], ["nvenc"])

            def inspect(path: Path) -> dict:
                result = subprocess.run(
                    [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
                     "-show_entries", "frame=best_effort_timestamp_time:stream=bit_rate",
                     "-of", "json", str(path)], check=True, capture_output=True, text=True,
                    creationflags=media_ffmpeg.NO_WINDOW)
                return json.loads(result.stdout)

            original, encoded = inspect(source), inspect(output)
            source_times = [float(frame["best_effort_timestamp_time"])
                            for frame in original["frames"]]
            output_times = [float(frame["best_effort_timestamp_time"])
                            for frame in encoded["frames"]]
            self.assertEqual(len(output_times), 2 * len(source_times))
            for offset in (0, len(source_times)):
                start = output_times[offset]
                for index, timestamp in enumerate(source_times):
                    self.assertAlmostEqual(output_times[offset + index] - start,
                                           timestamp - source_times[0], delta=0.002)
            self.assertLess(int(encoded["streams"][0]["bit_rate"]), target * 1.5)

    def test_border_detection_and_reflection_modes_keep_frames(self) -> None:
        """Обрезается только узкая кромка; режим чёрных краёв не отключает NVENC."""
        from black_border import detect_border
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            self.skipTest("FFprobe недоступен")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name, padding in (("plain", "null"), ("border", "pad=328:248:4:4"),
                                  ("wide", "pad=320:288:0:24")):
                subprocess.run(
                    [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                     "testsrc2=s=320x240:r=30", "-t", "2", "-vf", padding,
                     "-c:v", "libx264", "-crf", "12", "-y", str(folder / f"{name}.mp4")],
                    check=True, capture_output=True, creationflags=media_ffmpeg.NO_WINDOW)
            source = folder / "border.mp4"
            info = media_ffmpeg.probe_media(self.ffmpeg, source)
            self.assertEqual(detect_border(self.ffmpeg, source, info), (318, 238, 5, 5))
            for name in ("plain", "wide"):
                path = folder / f"{name}.mp4"
                self.assertIsNone(detect_border(self.ffmpeg, path, media_ffmpeg.probe_media(self.ffmpeg, path)))
            modes = [(False, True)]
            if media_ffmpeg.nvenc_available(self.ffmpeg):
                modes += [(True, False), (True, True)]
            for gpu, remove in modes:
                with self.subTest(gpu=gpu, remove=remove):
                    output = folder / f"output-{gpu}-{remove}.mp4"
                    with patch.object(self.worker, "request_cpu_fallback", return_value=False), \
                            patch.object(merge, "filtered_inputs", wraps=merge.filtered_inputs) as graph:
                        self.worker.run_merge(self.ffmpeg, [source], output, compress=True,
                                              use_gpu=gpu, stabilization="Слабый", deinterlace="Принудительно",
                                              crop_black_border=True, remove_stabilization_reflection=remove)
                    events = self.drain_events()
                    self.assertEqual(events[-1], ("merge_done", output), events)
                    self.assertEqual(graph.call_args.kwargs["gpu_stabilization"], gpu and not remove)
                    self.assertEqual(graph.call_args.kwargs["border_crops"], [(318, 238, 5, 5)])
                    self.assertEqual(any(kind == "stabilizing" for kind, _ in events), not gpu or remove)
                    self.assertEqual([data for kind, data in events if kind == "encoding"],
                                     ["nvenc" if gpu else "cpu"])
                    result = subprocess.run(
                        [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
                         "-show_entries", "stream=width,height,nb_read_frames,duration", "-of", "json", str(output)],
                        check=True, capture_output=True, text=True, creationflags=media_ffmpeg.NO_WINDOW)
                    stream = json.loads(result.stdout)["streams"][0]
                    self.assertEqual((stream["width"], stream["height"]), (328, 248))
                    self.assertEqual(int(stream["nb_read_frames"]), 120)
                    self.assertAlmostEqual(float(stream["duration"]), 2, delta=0.05)

    def test_nvidia_deinterlaces_on_gpu_with_and_without_stabilization(self) -> None:
        if not media_ffmpeg.nvenc_available(self.ffmpeg):
            self.skipTest("NVIDIA NVENC недоступен")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            interlaced = folder / "interlaced.mkv"
            progressive = folder / "progressive.mkv"
            self.make_video(interlaced, filter_graph="interlace=scan=bff,setfield=prog", rate="50")
            self.make_video(progressive)
            for stabilization in ("Выключен", "Слабый"):
                with self.subTest(stabilization=stabilization):
                    output = folder / f"output-{stabilization}.mkv"
                    with patch.object(self.worker, "request_cpu_fallback", return_value=False):
                        self.worker.run_merge(self.ffmpeg, [interlaced, progressive], output,
                                              compress=True, use_gpu=True, durations=[2, 2],
                                              total_seconds=4, stabilization=stabilization)
                    events = self.drain_events()
                    self.assertEqual(events[-1], ("merge_done", output), events)
                    self.assertEqual([data for kind, data in events if kind == "encoding"], ["nvenc"],
                                     (stabilization, events))
                    self.assertIsNone(media_ffmpeg.detect_field_order(self.ffmpeg, output, 4))

    def test_cuda_failure_requires_permission_before_cpu_retry(self) -> None:
        if not media_ffmpeg.nvenc_available(self.ffmpeg):
            self.skipTest("NVIDIA NVENC недоступен")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "source.mkv"
            output = folder / "output.mkv"
            self.make_video(source)
            original = merge.filtered_inputs

            def inputs(*args: object, **kwargs: object) -> list[str]:
                result = original(*args, **kwargs)
                if kwargs.get("gpu_deinterlace"):
                    index = result.index("-filter_complex") + 1
                    result[index] += ",missing_cuda_filter"
                return result

            with patch.object(merge, "detect_field_order", return_value="bff"), \
                    patch.object(merge, "filtered_inputs", side_effect=inputs), \
                    patch.object(self.worker, "request_cpu_fallback", return_value=False) as confirm:
                self.worker.run_merge(self.ffmpeg, [source], output,
                                      compress=True, use_gpu=True, deinterlace="Авто")
            events = self.drain_events()
            self.assertEqual(events[-1][0], "merge_error")
            self.assertFalse(output.exists())
            confirm.assert_called_once()
            with patch.object(merge, "detect_field_order", return_value="bff"), \
                    patch.object(merge, "filtered_inputs", side_effect=inputs), \
                    patch.object(self.worker, "request_cpu_fallback", return_value=True) as confirm:
                self.worker.run_merge(self.ffmpeg, [source], output,
                                      compress=True, use_gpu=True, deinterlace="Авто")
            events = self.drain_events()
            self.assertEqual(events[-1], ("merge_done", output), events)
            self.assertEqual([data for kind, data in events if kind == "encoding"], ["nvenc", "cpu"])
            confirm.assert_called_once()

    def test_opencl_failure_analyzes_stabilization_only_after_consent(self) -> None:
        if not media_ffmpeg.nvenc_available(self.ffmpeg):
            self.skipTest("NVIDIA NVENC недоступен")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source, output = folder / "source.mkv", folder / "output.mkv"
            self.make_video(source)
            original = merge.filtered_inputs

            def inputs(*args: object, **kwargs: object) -> list[str]:
                result = original(*args, **kwargs)
                if kwargs.get("gpu_stabilization"):
                    index = result.index("-filter_complex") + 1
                    result[index] += ",missing_opencl_filter"
                return result

            with patch.object(merge, "filtered_inputs", side_effect=inputs), \
                    patch.object(self.worker, "request_cpu_fallback", return_value=False), \
                    patch.object(self.worker, "analyze_stabilization",
                                 side_effect=AssertionError("Без согласия нельзя анализировать на CPU")):
                self.worker.run_merge(self.ffmpeg, [source], output, compress=True,
                                      use_gpu=True, deinterlace="Выключено",
                                      stabilization="Слабый", durations=[2])
            self.assertEqual(self.drain_events()[-1][0], "merge_error")
            self.assertFalse(output.exists())
            with patch.object(merge, "filtered_inputs", side_effect=inputs), \
                    patch.object(self.worker, "request_cpu_fallback", return_value=True):
                self.worker.run_merge(self.ffmpeg, [source], output, compress=True,
                                      use_gpu=True, deinterlace="Выключено",
                                      stabilization="Слабый", durations=[2])
            events = self.drain_events()
            self.assertEqual(events[-1], ("merge_done", output), events)
            self.assertEqual([data for kind, data in events if kind == "encoding"], ["nvenc", "cpu"])
            self.assertEqual([data for kind, data in events if kind == "stabilizing"], [(1, 1)])

    def test_stabilized_inputs_limit_decoder_and_filter_threads(self) -> None:
        paths = [Path(f"clip-{index}.mkv") for index in range(18)]
        inputs = merge.filtered_inputs(Path("manifest.txt"), paths, ["bff"] * len(paths), "Слабый")
        self.assertEqual(inputs.count("-threads:v"), len(paths))
        self.assertEqual(inputs.count("-filter_complex_threads"), 1)
        self.assertEqual(inputs[inputs.index("-filter_complex_threads") + 1], str(merge.FILTER_THREADS))
        self.assertEqual(inputs[inputs.index("-filter_complex") + 1].count("vidstabtransform="), len(paths))
        gpu_inputs = merge.filtered_inputs(Path("manifest.txt"), paths[:2],
                                           ["bff", None], "Слабый", gpu_deinterlace=True,
                                           gpu_stabilization=True)
        graph = gpu_inputs[gpu_inputs.index("-filter_complex") + 1]
        self.assertEqual(graph.count("bwdif_cuda="), 1)
        self.assertEqual(graph.count("deshake_opencl="), 2)
        self.assertEqual(graph.count("adaptive_crop=0"), 2)
        self.assertNotIn("vidstabtransform=", graph)
        self.assertIn("opencl=stabilizer:,device_vendor=NVIDIA", gpu_inputs)


if __name__ == "__main__":
    unittest.main()
