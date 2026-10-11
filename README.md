# Videoglue — склейка видео

[![Windows build](https://github.com/medneem/Videoglue/actions/workflows/build-windows.yml/badge.svg)](https://github.com/medneem/Videoglue/actions/workflows/build-windows.yml)

**Videoglue** — бесплатная программа с русским интерфейсом для объединения и обработки видео на Windows и Linux. Видео обрабатывается локально через FFmpeg. Код проекта распространяется по [MIT](LICENSE).

## Возможности

- Объединение роликов в выбранном порядке; добавление файлов кнопкой или перетаскиванием.
- Выход в **MP4** или **MKV**, копирование совместимых потоков без потери качества либо перекодирование в **H.264**.
- Выбор качества видео и аудио, частоты кадров и разрешения.
- Устранение чересстрочности, стабилизация, обрезка чёрной кромки и обработка отражений по краям.
- Кодирование на **CPU** или **NVIDIA NVENC** с подтверждением перехода на CPU при ошибке GPU.
- Предварительная оценка размера, отображение прогресса и журнал диагностики.

Входные форматы: `.mp4`, `.mov`, `.mkv`, `.avi`, `.webm`, `.m4v`. Поддержка конкретного файла зависит от его кодеков.

## Скачать и запустить

Windows x64: [GitHub Releases](https://github.com/medneem/Videoglue/releases). Распакуйте архив `Videoglue-vX.Y.N-windows-x64.zip` **в новую папку** и запустите `Videoglue.exe`. Python устанавливать не нужно.

**Внимание:** опубликованный ранее [v0.1.2](https://github.com/medneem/Videoglue/releases/tag/v0.1.2) собран с неподходящей версией FFmpeg; **стабилизация с этой комплектацией не гарантируется**. Для полноценной работы необходима **проверенная сборка FFmpeg 7.1.5**. Новые релизы собирают FFmpeg 7.1.5 из исходников прямо в CI.

В портативном дистрибутиве `ffmpeg.exe` и `ffprobe.exe` лежат **рядом с `Videoglue.exe`**, а не внутри него. Не удаляйте эти файлы. Папки `_internal` в архиве нет. Пользовательские настройки `config.jsonc` также хранятся рядом с EXE; для их сохранения при обновлении перенесите этот файл в новую папку.

## Как пользоваться

1. Добавьте видеоролики и перетащите строки в нужном порядке.
2. Галочками отметьте ролики для объединения.
3. Выберите MP4 или MKV, настройте сжатие, качество и обработку изображения.
4. Нажмите **«Склеить»** и укажите файл результата.

Исходные ролики не изменяются. После успешной обработки откроется папка результата.

**Режим «Сжать»:** при включении ролики перекодируются в H.264; доступны фильтры и NVIDIA. При выключении выполняется быстрое копирование потоков без фильтров, поэтому исходные кодеки и параметры должны быть совместимыми.

| Функция | CPU | NVIDIA |
|---|---|---|
| Кодирование H.264 | `libx264` | `h264_nvenc` |
| Деинтерлейсинг | `bwdif` | `bwdif_cuda` |
| Стабилизация | `vidstabdetect` + `vidstabtransform` | `deshake_opencl` |

Для GPU нужны совместимая видеокарта NVIDIA, драйвер и поддержка CUDA/OpenCL. Опция удаления отражений использует CPU-стабилизацию, даже если видеокодирование выполняется через NVENC. При ошибке GPU приложение запрашивает разрешение на повторную обработку на CPU.

## FFmpeg 7.1.5 — сборка из исходников

Videoglue требует **FFmpeg 7.1.5** с небольшим набором необходимых кодеков и фильтров. Готовые FFmpeg 8/9 ранее вызывали ошибки `deshake_opencl` на NVIDIA.

GitHub Actions собирает **статические Windows x64** `ffmpeg.exe` и `ffprobe.exe` на Ubuntu с помощью MinGW-w64, используя [`build_ffmpeg.sh`](build_ffmpeg.sh). Включены CPU-кодирование H.264, NVENC, CUDA-деинтерлейсинг и OpenCL/vid.stab-стабилизация.

| Зависимость | Закреплённая ревизия |
|---|---|
| FFmpeg | `n7.1.5` |
| x264 | [`b35605ace3ddf7c1a5d67a2eb553f034aef41d55`](https://github.com/mirror/x264/commit/b35605ace3ddf7c1a5d67a2eb553f034aef41d55) |
| vid.stab | `v1.1.1` |
| nv-codec-headers | `n12.2.72.0` |
| OpenCL Headers и ICD Loader | `v2024.10.24` |

**Этот хэш — Git-коммит x264, а не SHA-256 бинарника FFmpeg.** Хэши готовых EXE и ревизии исходников записываются в `FFMPEG_SOURCES.txt`.

[`ffmpeg-nvenc-rate.patch`](ffmpeg-nvenc-rate.patch) добавляет параметр `rc_framerate`, чтобы NVENC верно рассчитывал VBR после объединения видео с переменным FPS, не меняя временные метки. Если патч не применился, сборка завершится ошибкой.

Локальная сборка из Ubuntu/WSL (после установки Git, MinGW-w64, Make, NASM, CMake, Clang/LLVM и pkg-config):

```bash
JOBS=4 bash build_ffmpeg.sh
```

Готовые файлы: `~/videoglue-ffmpeg-build/output/ffmpeg.exe` и `ffprobe.exe`.

## Разработка и тестирование

**Windows** — Python 3.13+ с Tcl/Tk и проверенный `ffmpeg.exe` рядом с исходниками:

```powershell
git clone https://github.com/medneem/Videoglue.git
cd Videoglue
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

**Linux** — Python 3.13+, tkinter и FFmpeg в `PATH` с необходимыми фильтрами:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

Запуск тестов (из подготовленного Windows-окружения):

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_main test_release
```

GUI-тестам нужен графический рабочий стол; интеграционные тесты требуют FFmpeg, а некоторые — `ffprobe` в `PATH`. Аппаратные проверки пропускаются, если NVENC недоступен.

Локальная сборка Windows EXE:

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean Videoglue.spec
```

Результат — `dist\Videoglue.exe` (режим `onefile`). Скопируйте рядом с ним проверенные `ffmpeg.exe` и `ffprobe.exe`; в EXE они не встраиваются.

## GitHub Actions: версии и релизы

[Windows pipeline](https://github.com/medneem/Videoglue/actions/workflows/build-windows.yml) запускается при push в `master`, в pull request и вручную. Версия — `0.1.<номер запуска>`; например, запуск №42 создаёт тег `v0.1.42` и ZIP `Videoglue-v0.1.42-windows-x64.zip`.

1. **Ubuntu:** кросс-компиляция FFmpeg и FFprobe 7.1.5 из закреплённых исходников, фиксация SHA-256 и коммитов.
2. **Windows:** проверка версии, фильтров, NVENC-патча, запуск тестов и PyInstaller `--onefile`.
3. **Публикация:** проверка содержимого ZIP, загрузка в Actions → Artifacts (14 дней) и, после успешной сборки ветки `master`, в GitHub Releases.

В ZIP содержатся `Videoglue.exe`, `ffmpeg.exe`, `ffprobe.exe`, сведения о версиях и исходниках, скрипт и патч для FFmpeg, а также лицензии. Все файлы находятся рядом с EXE, без папки `_internal`. **Не нужны ручная загрузка бинарников FFmpeg, отдельный SHA-256 ZIP или служебный релиз.**

CI проверяет поддержку требуемых функций, но не тестирует работу стабилизации на реальной видеокарте NVIDIA. После первой сборки следует выполнить такой тест вручную.

## Диагностика и лицензии

- `config.jsonc` — локальные настройки; `Videoglue.log` — журнал команд FFmpeg, ошибок и переходов на CPU. Файлы хранятся рядом с EXE (при запуске исходников — в папке проекта).
- Собственный код Videoglue — [MIT](LICENSE). FFmpeg, x264 и другие зависимости распространяются на собственных условиях, включая применимые требования GPL. См. [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) и [FFmpeg Legal](https://ffmpeg.org/legal.html). При публикации собственной GPL-сборки необходимо обеспечить выполнение обязательств по соответствующим исходникам и патчам.
