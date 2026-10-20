# Сторонние компоненты

Код Videoglue распространяется по [MIT](LICENSE).

В портативный Windows ZIP входят собственные статические сборки `ffmpeg.exe` и
`ffprobe.exe` версии **7.1.5**. Они создаются на Ubuntu через MinGW-w64
по [`build_ffmpeg.sh`](build_ffmpeg.sh) из закреплённых исходников.
Архив включает `FFMPEG_SOURCES.txt` (версии и SHA-256), `FFMPEG_BUILD.sh`
и `FFMPEG_NVENC.patch`.

При сборке FFmpeg с x264 и vid.stab включена лицензия GPL.
Копия GPLv3 поставляется как `LICENSE_FFMPEG.txt`.
Библиотека `dav1d` (декодирование AV1 на CPU) распространяется по BSD-2-Clause.
Её лицензия включена в ZIP как `LICENSE_DAV1D.txt`. При распространении
исполняемых файлов следует предоставить соответствующие исходники,
изменения и информацию о сборке согласно действующим условиям лицензий.

Источники: [FFmpeg](https://github.com/FFmpeg/FFmpeg),
[x264](https://code.videolan.org/videolan/x264),
[vid.stab](https://github.com/georgmartius/vid.stab),
[dav1d](https://github.com/videolan/dav1d),
[nv-codec-headers](https://github.com/FFmpeg/nv-codec-headers),
[OpenCL-Headers](https://github.com/KhronosGroup/OpenCL-Headers),
[OpenCL-ICD-Loader](https://github.com/KhronosGroup/OpenCL-ICD-Loader).

Python, Tcl/Tk, tkinterdnd2 и PyInstaller распространяются по собственным лицензиям.
