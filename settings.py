"""Чтение и сохранение пользовательских настроек рядом с программой."""

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


FRAME_RATES = ("Оригинал", "Авто")
AUDIO_QUALITIES = ("Оригинал", "AAC 128 кбит/с", "AAC 192 кбит/с", "AAC 256 кбит/с")
RESOLUTIONS = ("Как у первого ролика", "1080p", "720p")
SETTINGS_VERSION = 2


@dataclass
class AppSettings:
    """Настройки окна и обработки; каталог входа не означает загрузку файлов."""

    input_dir: str = ""
    output_dir: str = ""
    format: str = "MP4"
    compress: bool = True
    quality: str = "Максимальное"
    frame_rate: str = "Оригинал"
    audio_quality: str = "Оригинал"
    resolution: str = "Как у первого ролика"
    deinterlace: str = "Авто"
    stabilization: str = "Выключен"
    crop_black_border: bool = False
    remove_stabilization_reflection: bool = False
    use_gpu: bool = True
    width: int = 920
    height: int = 600
    x: int | None = None
    y: int | None = None
    settings_version: int = SETTINGS_VERSION


def config_path() -> Path:
    """В EXE использовать его каталог, а не временную распаковку PyInstaller."""
    directory = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    return directory / "config.jsonc"


def strip_jsonc(text: str) -> str:
    """Удалить комментарии и конечные запятые, не меняя содержимое строк JSON.

    Поддерживаются //, /* ... */ и запятая перед } или ]; строки и
    экранированные кавычки обрабатываются отдельно от комментариев.
    """
    result: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
        elif char == '"':
            in_string = True
            result.append(char)
            index += 1
        elif char == "/" and following == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
        elif char == "/" and following == "*":
            index += 2
            while index + 1 < len(text) and text[index:index + 2] != "*/":
                index += 1
            if index + 1 >= len(text):
                raise ValueError("Незакрытый комментарий в config.jsonc")
            index += 2
        else:
            result.append(char)
            index += 1

    # Отдельный проход не удаляет запятые, находящиеся внутри строк.
    clean = "".join(result)
    result = []
    in_string = False
    escaped = False
    for index, char in enumerate(clean):
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            result.append(char)
        elif char == ",":
            # Не копируем весь остаток документа для каждой запятой.
            following = index + 1
            while following < len(clean) and clean[following].isspace():
                following += 1
            if following >= len(clean) or clean[following] not in "}]":
                result.append(char)
        else:
            result.append(char)
    return "".join(result)


def load_settings(path: Path | None = None) -> AppSettings:
    """Прочитать допустимые значения, сохранив умолчания для повреждённых полей."""
    settings = AppSettings()
    try:
        data = json.loads(strip_jsonc((path or config_path()).read_text(encoding="utf-8-sig")))
    except (OSError, ValueError):
        return settings
    if not isinstance(data, dict):
        return settings

    for key in ("input_dir", "output_dir"):
        value = data.get(key)
        if isinstance(value, str):
            setattr(settings, key, value)
    if data.get("format") in ("MP4", "MKV"):
        settings.format = data["format"]
    if data.get("quality") in ("Максимальное", "Высокое", "Компактное", "Малое"):
        settings.quality = data["quality"]
    if data.get("frame_rate") in FRAME_RATES:
        settings.frame_rate = data["frame_rate"]
    if data.get("audio_quality") in AUDIO_QUALITIES:
        settings.audio_quality = data["audio_quality"]
    if data.get("resolution") in RESOLUTIONS:
        settings.resolution = data["resolution"]
    if data.get("deinterlace") in ("Авто", "Принудительно", "Выключено"):
        settings.deinterlace = data["deinterlace"]
    if data.get("stabilization") in ("Выключен", "Слабый", "Средний", "Сильный"):
        settings.stabilization = data["stabilization"]
    # Старые конфиги один раз включают сжатие; после сохранения версия
    # позволяет пользователю снова выключать его без сброса при запуске.
    if type(data.get("settings_version")) is int and data["settings_version"] >= SETTINGS_VERSION:
        if type(data.get("compress")) is bool:
            settings.compress = data["compress"]
    for key in ("use_gpu", "crop_black_border", "remove_stabilization_reflection"):
        if type(data.get(key)) is bool:
            setattr(settings, key, data[key])
    for key, minimum in (("width", 850), ("height", 520)):
        value = data.get(key)
        if type(value) is int and minimum <= value <= 10000:
            setattr(settings, key, value)
    for key in ("x", "y"):
        value = data.get(key)
        if type(value) is int and -100000 <= value <= 100000:
            setattr(settings, key, value)
    return settings


def save_settings(settings: AppSettings, path: Path | None = None) -> None:
    """Записать документ целиком, атомарно заменив старый конфиг."""
    destination = path or config_path()
    content = (
        "// Настройки Videoglue. Допускаются комментарии и запятые после последнего поля.\n"
        "// При сохранении из программы файл переписывается; пользовательские комментарии не сохраняются.\n"
        + json.dumps(vars(settings), ensure_ascii=False, indent=2) + "\n"
    )
    temporary: str | None = None
    try:
        # Временный файл в том же каталоге позволяет заменить конфиг атомарно.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=".config-", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            stream.write(content)
        os.replace(temporary, destination)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
