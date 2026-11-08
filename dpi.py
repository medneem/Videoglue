"""DPI awareness for Windows and sizing of pixel-based Tk elements."""
import ctypes
import sys


def enable_windows_dpi_awareness() -> None:
    """Disable bitmap upscaling before the first Tk/Win32 window is created."""
    if sys.platform != "win32":
        return
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def scaled_pixels(pixels: int, tk_scaling: float) -> int:
    """Convert 96-DPI design pixels to device pixels; font points scale in Tk."""
    return max(1, round(pixels * float(tk_scaling) / (96 / 72)))
