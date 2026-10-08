"""Ограниченный диагностический журнал рядом с EXE, безопасный при отказе записи."""

import logging
import subprocess
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path


class DebugJournal:
    """Писать команды и диагностику без вывода ошибок журнала в консоль/Tk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.failed = False
        self.lock = threading.RLock()
        self.logger = logging.Logger("Videoglue", level=logging.DEBUG)
        self.logger.propagate = False
        self.handler: RotatingFileHandler | None = None

    def write(self, message: str, *, exception: bool = False) -> None:
        """Создать журнал лениво; ошибки диска не прерывают обработку видео."""
        with self.lock:
            try:
                if self.handler is None:
                    self.handler = RotatingFileHandler(self.path, maxBytes=5 * 1024 * 1024,
                                                        backupCount=3, encoding="utf-8")
                    self.handler.setFormatter(logging.Formatter("%(asctime)s [%(threadName)s] %(message)s"))
                    self.handler.handleError = lambda _record: setattr(self, "failed", True)
                    self.logger.addHandler(self.handler)
                if exception:
                    message += "\n" + traceback.format_exc()
                for start in range(0, max(1, len(message)), 32768):
                    self.logger.info(message[start:start + 32768])
            except Exception:
                self.failed = True

    def command(self, command: list[str], cwd: str | None = None) -> None:
        """Сохранить полную команду с корректным экранированием Windows."""
        self.write(f"COMMAND cwd={cwd!r}\n{subprocess.list2cmdline(command)}")

    def close(self) -> None:
        """Освободить файл (нужно также для изолированных проверок журнала)."""
        with self.lock:
            if self.handler is not None:
                self.logger.removeHandler(self.handler)
                self.handler.close()
                self.handler = None

    def notice(self) -> str:
        """Дать путь к диагностике или честно сообщить отказ записи."""
        if self.failed:
            return "Не удалось записать журнал. Проверьте доступ на запись в папку программы."
        return f"Подробности сохранены в журнале:\n{self.path}"


_directory = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
journal = DebugJournal(_directory / "Videoglue.log")
_versions: set[str] = set()
_version_lock = threading.Lock()


def log_ffmpeg_version(ffmpeg: str) -> None:
    """Один раз записать версию и параметры сборки выбранного FFmpeg."""
    with _version_lock:
        if ffmpeg in _versions:
            return
        try:
            result = subprocess.run([ffmpeg, "-version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            journal.write("FFMPEG VERSION\n" + result.stdout.decode("utf-8", "replace"))
            _versions.add(ffmpeg)
        except (OSError, subprocess.TimeoutExpired):
            journal.write("Не удалось прочитать версию FFmpeg", exception=True)
