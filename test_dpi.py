"""Windows DPI setup and physical-pixel sizing regression tests."""
import unittest
from unittest.mock import patch

import dpi


class DpiTests(unittest.TestCase):
    def test_non_windows_does_not_call_win32(self):
        with patch.object(dpi.sys, "platform", "linux"), patch.object(dpi.ctypes, "windll", create=True) as native:
            dpi.enable_windows_dpi_awareness()
            native.assert_not_called()

    def test_prefers_per_monitor_v2(self):
        with patch.object(dpi.sys, "platform", "win32"), patch.object(dpi.ctypes, "windll", create=True) as native:
            native.user32.SetProcessDpiAwarenessContext.return_value = 1
            dpi.enable_windows_dpi_awareness()
            self.assertEqual(native.user32.SetProcessDpiAwarenessContext.call_args.args[0].value,
                             dpi.ctypes.c_void_p(-4).value)
            native.shcore.SetProcessDpiAwareness.assert_not_called()
            native.user32.SetProcessDPIAware.assert_not_called()

    def test_falls_back_to_windows_81_api(self):
        with patch.object(dpi.sys, "platform", "win32"), patch.object(dpi.ctypes, "windll", create=True) as native:
            native.user32.SetProcessDpiAwarenessContext.side_effect = AttributeError("not supported")
            native.shcore.SetProcessDpiAwareness.return_value = 0
            dpi.enable_windows_dpi_awareness()
            native.shcore.SetProcessDpiAwareness.assert_called_once_with(2)
            native.user32.SetProcessDPIAware.assert_not_called()

    def test_falls_back_to_vista_api(self):
        with patch.object(dpi.sys, "platform", "win32"), patch.object(dpi.ctypes, "windll", create=True) as native:
            native.user32.SetProcessDpiAwarenessContext.return_value = 0
            native.shcore.SetProcessDpiAwareness.side_effect = OSError("not supported")
            dpi.enable_windows_dpi_awareness()
            native.user32.SetProcessDPIAware.assert_called_once_with()

    def test_pixel_values_scale_with_tk_dpi(self):
        self.assertEqual(dpi.scaled_pixels(30, 96 / 72), 30)
        self.assertEqual(dpi.scaled_pixels(30, 192 / 72), 60)
        self.assertEqual(dpi.scaled_pixels(20, 144 / 72), 30)
