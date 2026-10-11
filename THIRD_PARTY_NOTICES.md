# Сторонние компоненты

Собственный код Videoglue лицензирован под MIT — см. [LICENSE](LICENSE).

Windows ZIP должен включать **проверенную FFmpeg 7.1.5** в виде отдельных
`ffmpeg.exe` и `ffprobe.exe` — ни плавающий latest, ни FFmpeg 8/9
для стабилизатора Videoglue не подходят. Архив источника зафиксирован SHA-256,
конкретная версия бинарников записывается в `FFMPEG_VERSION.txt`.

Для собственной GPL-сборки FFmpeg (x264 и vid.stab) распространяются
применимые условия GPL. Копия GPLv3 включена в ZIP как
`LICENSE_FFMPEG.txt`. Должны быть выполнены также требования к
соответствующим исходникам, патчам и сборочным инструкциям.

FFmpeg: https://ffmpeg.org/
Официальные исходники FFmpeg 7.1.5: https://ffmpeg.org/releases/
Python, Tcl/Tk, tkinterdnd2 и PyInstaller имеют собственные лицензии.
