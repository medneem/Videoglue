"""Release gate: Videoglue supports only the tested FFmpeg 7.1.5 toolchain.

Exits nonzero when ffmpeg/ffprobe versions, codecs or filters differ.
Binaries are built from pinned sources in CI. GPU hardware still requires real-world testing.
"""

from __future__ import annotations

import base64
import re
import subprocess
import tempfile
import sys
from pathlib import Path

REQUIRED_VERSION = "7.1.5"
REQUIRED_FILTERS = ("vidstabdetect", "vidstabtransform", "deshake_opencl", "bwdif_cuda")
REQUIRED_ENCODER = "h264_nvenc"
REQUIRED_DECODER = "libdav1d"
# One black 16x16 AV1 frame in IVF format (libaom-av1).
# Inline data lets release CI check decoding without downloading media.
AV1_SAMPLE_IVF_B64 = (
    "REtJRgAAIABBVjAxEAAQAAEAAAABAAAAAQAAAAAAAAAcAAAAAAAAAAAAAAAS"
    "AAoKAAAAAZ/5tfIAgDIMEAD/gAACwAAAAKmB"
)


def has_software_av1_decoder(output: str) -> bool:
    """The built-in av1 entry alone cannot decode AV1 on all hardware."""
    return bool(re.search(r"(?m)^\s*V\S*\s+libdav1d\b", output))



def matches_required_version(program: str, output: str) -> bool:
    """Accept 7.1.5 or n7.1.5, never 7.1 or 7.1.50 or 8/9."""
    first_line = output.splitlines()[0].strip() if output.splitlines() else ""
    return bool(re.match(
        rf"^{re.escape(program)} version n?{re.escape(REQUIRED_VERSION)}(?=[\s-]|$)",
        first_line,
    ))


def capture(executable: Path, *args: str) -> str:
    result = subprocess.run(
        [str(executable), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=40,
        check=True,
    )
    return result.stdout


def validate(ffmpeg: Path, ffprobe: Path) -> str:
    for program, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        if not path.is_file():
            raise ValueError(f"{program}.exe not found: {path}")
        version = capture(path, "-version")
        if not matches_required_version(program, version):
            found = version.splitlines()[0] if version.splitlines() else "(no version)"
            raise ValueError(
                f"Videoglue requires {program} version {REQUIRED_VERSION}; got {found!r}"
            )

    filters = capture(ffmpeg, "-hide_banner", "-filters")
    for required in REQUIRED_FILTERS:
        if not re.search(rf"\b{re.escape(required)}\b", filters):
            raise ValueError(f"FFmpeg {REQUIRED_VERSION}: missing filter {required}")

    encoders = capture(ffmpeg, "-hide_banner", "-encoders")
    if not re.search(rf"\b{REQUIRED_ENCODER}\b", encoders):
        raise ValueError(f"FFmpeg {REQUIRED_VERSION}: missing {REQUIRED_ENCODER}")

    decoders = capture(ffmpeg, "-hide_banner", "-decoders")
    if not has_software_av1_decoder(decoders):
        raise ValueError(f"FFmpeg {REQUIRED_VERSION}: missing {REQUIRED_DECODER}")

    # Test a real bitstream, and ensure auto-selection uses dav1d too.
    with tempfile.TemporaryDirectory() as temp_dir:
        sample = Path(temp_dir) / "av1-smoke.ivf"
        sample.write_bytes(base64.b64decode(AV1_SAMPLE_IVF_B64))
        capture(ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-c:v", REQUIRED_DECODER, "-i", str(sample),
                "-frames:v", "1", "-an", "-f", "null", "-")
        auto_output = capture(ffmpeg, "-hide_banner", "-nostdin",
                              "-i", str(sample), "-frames:v", "1",
                              "-an", "-f", "null", "-")
        if not re.search(r"av1 \(libdav1d\) ->", auto_output):
            raise ValueError("FFmpeg is not selecting libdav1d for AV1 input")

    options = capture(ffmpeg, "-hide_banner", "-h", "encoder=h264_nvenc")
    if not re.search(r"\brc_framerate\b", options):
        raise ValueError("FFmpeg missing Videoglue NVENC rc_framerate patch")

    return capture(ffmpeg, "-version").splitlines()[0]


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: check_ffmpeg_toolchain.py ffmpeg.exe ffprobe.exe")
    try:
        version = validate(Path(sys.argv[1]), Path(sys.argv[2]))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Rejected incompatible FFmpeg toolchain: {error}") from error
    print("Verified pinned Videoglue toolchain:", version)
