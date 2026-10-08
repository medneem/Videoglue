"""Консервативная проверка узкой кромки без cropdetect и внешних библиотек."""

import struct
import tempfile
from pathlib import Path

from debug_log import journal
from ffmpeg import MediaInfo, run_diagnostic


def avi_frame(data: bytes) -> tuple[int, int, bytes]:
    """Прочитать единственный несжатый BGR24-кадр из AVI, включая padding строк."""
    bitmap = None
    pixels = None

    def chunks(start: int, end: int) -> None:
        nonlocal bitmap, pixels
        while start + 8 <= end:
            kind = data[start:start + 4]
            size = struct.unpack_from("<I", data, start + 4)[0]
            body = start + 8
            limit = min(body + size, end)
            if kind in (b"RIFF", b"LIST"):
                chunks(body + 4, limit)
            elif kind == b"strf" and size >= 40:
                header = struct.unpack_from("<IiiHHI", data, body)
                if header[4] == 24 and header[5] == 0:
                    bitmap = (header[1], header[2])
            elif kind.endswith((b"db", b"dc")) and pixels is None:
                pixels = data[body:limit]
            start = body + size + (size & 1)

    chunks(0, len(data))
    if bitmap is None or pixels is None:
        raise ValueError("Не удалось прочитать BGR24-кадр AVI")
    width, signed_height = bitmap
    height = abs(signed_height)
    if width <= 0 or height <= 0:
        raise ValueError("Недопустимые размеры кадра")
    stride = (width * 3 + 3) & ~3
    if len(pixels) != stride * height:
        raise ValueError("Неполный кадр AVI")
    rows = [pixels[row * stride:row * stride + width * 3] for row in range(height)]
    if signed_height > 0:
        rows.reverse()
    return width, height, b"".join(rows)


def frame_edges(width: int, height: int, pixels: bytes) -> tuple[int, int, int, int] | None:
    """Найти только почти сплошные тёмные края, не принимая широкие полосы за кромку."""
    def black_line(indices: range) -> bool:
        dark = sum(max(pixels[index * 3:index * 3 + 3]) <= 24 for index in indices)
        return dark >= len(indices) * 0.985

    limits = (min(16, max(2, width // 40)), min(16, max(2, height // 40)))
    edges = []
    for side in range(4):
        limit = limits[0 if side < 2 else 1]
        count = 0
        for offset in range(limit + 1):
            if side == 0:
                indices = range(offset, width * height, width)
            elif side == 1:
                indices = range(width - 1 - offset, width * height, width)
            elif side == 2:
                indices = range(offset * width, (offset + 1) * width)
            else:
                indices = range((height - 1 - offset) * width, (height - offset) * width)
            if not black_line(indices):
                break
            count += 1
        # Слишком широкая полоса или тёмный кадр: ничего не обрезаем.
        if count > limit:
            return None
        edges.append(count)
    return tuple(edges)


def detect_border(ffmpeg: str, path: Path, info: MediaInfo, *, stopping=None,
                  track_process=None) -> tuple[int, int, int, int] | None:
    """Проверить три разнесённых кадра; вернуть crop=(w,h,x,y) только при согласии."""
    if not info.width or not info.height or not info.duration:
        return None
    measurements = []
    with tempfile.TemporaryDirectory() as directory:
        sample = Path(directory) / "frame.avi"
        for fraction in (0.1, 0.5, 0.9):
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                       "-ss", str(info.duration * fraction), "-threads:v", "1", "-i", str(path),
                       "-map", "0:v:0", "-frames:v", "1", "-an", "-c:v", "rawvideo",
                       "-pix_fmt", "bgr24", "-f", "avi", str(sample)]
            code, _ = run_diagnostic(command, 20, stopping=stopping, track_process=track_process)
            if code:
                journal.write(f"BORDER sample failed: {path}")
                return None
            try:
                width, height, pixels = avi_frame(sample.read_bytes())
                edges = frame_edges(width, height, pixels)
            except ValueError:
                journal.write(f"BORDER invalid sample: {path}", exception=True)
                return None
            if edges is None or (width, height) != (info.width, info.height):
                return None
            measurements.append(edges)
    # Отличающиеся на много пикселей края означают границу сцены, а не рамку.
    if any(max(values) - min(values) > 2 for values in zip(*measurements)):
        return None
    # Допуск в два пикселя учитывает шум DV; берём внешний предел рамки,
    # иначе оставшаяся чёрная строка снова попадёт в отражение стабилизатора.
    left, right, top, bottom = (max(values) for values in zip(*measurements))
    if not any((left, right, top, bottom)):
        return None
    # Один пиксель запаса убирает смешанную с изображением границу после
    # компрессии/деинтерлейсинга; края без найденной рамки не затрагиваем.
    left, right, top, bottom = (edge + 1 if edge else 0 for edge in (left, right, top, bottom))
    width = (info.width - left - right) // 2 * 2
    height = (info.height - top - bottom) // 2 * 2
    result = (width, height, left, top)
    journal.write(f"BORDER {path}: samples={measurements}, crop={result}")
    return result
