"""Воспроизводимое сравнение скорости фильтров на синтетическом видео.

Команда: py -3.14 benchmark_stabilization.py. Пользовательские видео не
открываются, все материалы создаются и удаляются во временном каталоге.
"""

import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from ffmpeg import NO_WINDOW, ffmpeg_path, nvenc_available
from merge import MergeWorker, concat_entry, filtered_inputs


def old_inputs(inputs: list[str]) -> list[str]:
    """Восстановить прежнюю команду с неограниченными пулами и bicubic."""
    previous: list[str] = []
    index = 0
    while index < len(inputs):
        if inputs[index] in ("-threads:v", "-filter_complex_threads"):
            index += 2
            continue
        previous.append(inputs[index].replace("interpol=bilinear", "interpol=bicubic"))
        index += 1
    return previous


def main() -> None:
    """Создать несколько DV-подобных фрагментов и измерить кодирование."""
    ffmpeg = ffmpeg_path()
    if not nvenc_available(ffmpeg):
        raise RuntimeError("NVENC недоступен — тест скорости кодирования невозможен")
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        source = folder / "source.mkv"
        # Чересстрочное видео 720×576: после bwdif кадры будут 50 fps.
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "testsrc2=s=720x576:r=50", "-t", "2", "-vf",
             "interlace=scan=bff", "-c:v", "mpeg4", "-y", str(source)],
            capture_output=True, check=True, creationflags=NO_WINDOW,
        )
        videos = [source] * 8
        worker = MergeWorker(queue.SimpleQueue(), threading.Event())
        if not worker.analyze_stabilization(ffmpeg, videos, ["bff"] * len(videos), directory, [2] * len(videos)):
            raise RuntimeError("Анализ стабилизации остановлен")
        manifest = folder / "videos.txt"
        manifest.write_text("".join(concat_entry(path) for path in videos), encoding="utf-8")
        optimized = filtered_inputs(manifest, videos, ["bff"] * len(videos), "Слабый")
        # Прогреваем NVENC и запускаем оба варианта на одних и тех же кадрах.
        for label, inputs in (("Новый граф", optimized), ("Старый граф", old_inputs(optimized))):
            output = folder / ("new.mp4" if label == "Новый граф" else "old.mp4")
            started = time.perf_counter()
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                 *inputs, "-c:v", "h264_nvenc", "-preset", "p5", "-cq", "17",
                 "-b:v", "0", "-an", "-fps_mode", "vfr", str(output)],
                capture_output=True, cwd=directory, creationflags=NO_WINDOW,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.decode("utf-8", "replace"))
            print(f"{label}: {time.perf_counter() - started:.2f} с, {output.stat().st_size} байт")


if __name__ == "__main__":
    main()
