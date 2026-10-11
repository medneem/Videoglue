"""Validate version stamping and FFmpeg beside a frozen EXE."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ffmpeg as media_ffmpeg
from scripts.stamp_version import make_version, stamp_version
from scripts.check_ffmpeg_toolchain import matches_required_version


class ReleaseTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(make_version("0.1", "42"), "0.1.42")
        for base, run in [("0.1.0", "3"), ("0.1", "0"), ("0.1", "65536")]:
            self.assertRaises(ValueError, make_version, base, run)

    def test_ffmpeg_715_only(self):
        self.assertTrue(matches_required_version("ffmpeg", "ffmpeg version 7.1.5 Copyright"))
        self.assertTrue(matches_required_version("ffmpeg", "ffmpeg version n7.1.5 Copyright"))
        self.assertTrue(matches_required_version("ffprobe", "ffprobe version 7.1.5 Copyright"))
        for version in ("7.1", "7.1.4", "7.1.50", "8.1.2", "9.0.2"):
            self.assertFalse(matches_required_version("ffmpeg", f"ffmpeg version {version} Copyright"))
        self.assertFalse(matches_required_version("ffprobe", "ffprobe version 7.1.4 Copyright"))

    def test_version_resource(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(stamp_version("0.1", "42", root), "0.1.42")
            self.assertIn('0.1.42', (root / "version.py").read_text())
            self.assertIn("0.1.42.0", (root / "windows_version_info.txt").read_text())

    def test_ffmpeg_beside_exe(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / "Videoglue.exe"
            ffmpeg = Path(folder) / "ffmpeg.exe"
            ffmpeg.touch()
            with patch.object(media_ffmpeg.sys, "platform", "win32"), \
                 patch.object(media_ffmpeg.sys, "frozen", True, create=True), \
                 patch.object(media_ffmpeg.sys, "executable", str(exe)), \
                 patch.object(media_ffmpeg.shutil, "which", return_value=None):
                self.assertEqual(Path(media_ffmpeg.ffmpeg_path()).resolve(), ffmpeg.resolve())


if __name__ == "__main__":
    unittest.main()
