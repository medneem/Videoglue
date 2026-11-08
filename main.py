"""Точка входа приложения для исходников и сборки Windows EXE."""

# Блок главного окна: собирает интерфейс, таблицу роликов и настройки,
# соединяет фоновую проверку FFmpeg и склейку через очередь событий.
from dpi import enable_windows_dpi_awareness


if __name__ == "__main__":
    # Windows must know the real monitor DPI before Tk creates its first window.
    enable_windows_dpi_awareness()
    from window import VideoGlue

    VideoGlue().mainloop()
