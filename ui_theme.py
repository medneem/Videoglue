"""Единая палитра и стили tkinter/ttk для Videoglue."""

import sys
from tkinter import ttk


THEMES = ("Светлая", "Тёмная", "Системная")

LIGHT = {
    "bg": "#f4f7fb",
    "panel": "#ffffff",
    "text": "#15233b",
    "muted": "#61718a",
    "accent": "#0868ed",
    "accent_hover": "#0057d4",
    "border": "#d5e0ed",
    "header": "#ecf3fb",
    "field": "#ffffff",
    "disabled": "#a0adc0",
    "stripe": "#f3f7fc",
    "selection": "#d9eaff",
    "drop_target": "#c9e3ff",
    "drop_gap": "#e4f1ff",
    "arrive": "#d9f5e5",
    "drag_source": "#e4eaf2",
    "remove_press": "#ffcad3",
    "remove_fade": "#fbe9ec",
    "drop_active": "#80b8ff",
    "error": "#ba3547",
}

DARK = {
    "bg": "#151c27",
    "panel": "#202a38",
    "text": "#e9eff9",
    "muted": "#a1b2c8",
    "accent": "#438fff",
    "accent_hover": "#2875ea",
    "border": "#3b4a60",
    "header": "#2a3648",
    "field": "#263245",
    "disabled": "#7c8aa0",
    "stripe": "#253244",
    "selection": "#335b86",
    "drop_target": "#315f8b",
    "drop_gap": "#315575",
    "arrive": "#27553f",
    "drag_source": "#37485e",
    "remove_press": "#764150",
    "remove_fade": "#533945",
    "drop_active": "#4c85cc",
    "error": "#ff8494",
}


def system_theme() -> str:
    """Прочитать настройку Windows; на других ОС использовать светлую тему."""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return "Тёмная" if value == 0 else "Светлая"
        except OSError:
            pass
    return "Светлая"


def palette(theme: str) -> dict[str, str]:
    """Получить цвета без изменения сохранённого выбора «Системная»."""
    selected = system_theme() if theme == "Системная" else theme
    return (DARK if selected == "Тёмная" else LIGHT).copy()


def apply_theme(root, selected: str) -> dict[str, str]:
    """Настроить все ttk-виджеты без сторонних библиотек оформления."""
    colors = palette(selected)
    style = ttk.Style(root)
    style.theme_use("clam")
    bg, panel, text = colors["bg"], colors["panel"], colors["text"]
    border, accent = colors["border"], colors["accent"]
    root.configure(background=bg)

    style.configure(".", font=("Segoe UI", 10), background=bg, foreground=text)
    style.configure("TFrame", background=bg)
    style.configure("Card.TFrame", background=panel)
    style.configure("TLabel", background=bg, foreground=text)
    style.configure("Muted.TLabel", background=bg, foreground=colors["muted"])
    style.configure("Section.TLabel", background=bg, foreground=accent,
                    font=("Segoe UI", 10, "bold"))
    style.configure("Card.TLabel", background=panel, foreground=text)
    style.configure("TLabelFrame", background=panel, bordercolor=border,
                    relief="solid", borderwidth=1)
    style.configure("TLabelFrame.Label", background=bg, foreground=accent,
                    font=("Segoe UI", 10, "bold"))
    style.configure("TCheckbutton", background=bg, foreground=text)
    style.configure("Card.TCheckbutton", background=panel, foreground=text)
    for check in ("TCheckbutton", "Card.TCheckbutton"):
        style.map(check, foreground=[("disabled", colors["disabled"])],
                  background=[("active", panel if check.startswith("Card") else bg)])

    style.configure("TButton", background=panel, foreground=text,
                    bordercolor=border, borderwidth=1, relief="flat", padding=(14, 7))
    style.map("TButton",
              background=[("disabled", bg), ("pressed", colors["header"]),
                          ("active", colors["header"])],
              foreground=[("disabled", colors["disabled"])])
    style.configure("Primary.TButton", background=accent, foreground="#ffffff",
                    bordercolor=accent, borderwidth=1, relief="flat",
                    font=("Segoe UI", 10, "bold"), padding=(18, 9))
    style.map("Primary.TButton",
              background=[("disabled", colors["header"]),
                          ("pressed", colors["accent_hover"]),
                          ("active", colors["accent_hover"])],
              foreground=[("disabled", colors["disabled"]), ("!disabled", "#ffffff")])

    style.configure("TCombobox", fieldbackground=colors["field"],
                    background=colors["field"], foreground=text,
                    arrowcolor=colors["muted"], bordercolor=border,
                    lightcolor=border, darkcolor=border, relief="flat", padding=(7, 4))
    style.map("TCombobox",
              fieldbackground=[("readonly", colors["field"]), ("disabled", bg)],
              foreground=[("disabled", colors["disabled"]), ("readonly", text)],
              selectbackground=[("readonly", colors["field"])],
              selectforeground=[("readonly", text)])
    root.option_add("*TCombobox*Listbox.background", colors["field"])
    root.option_add("*TCombobox*Listbox.foreground", text)

    style.configure("Video.Treeview", rowheight=32, background=panel,
                    fieldbackground=panel, foreground=text,
                    bordercolor=border, borderwidth=0, font=("Segoe UI", 10))
    style.map("Video.Treeview",
              background=[("selected", colors["selection"])],
              foreground=[("selected", text)])
    style.configure("Video.Treeview.Heading", background=colors["header"],
                    foreground=text, relief="flat", bordercolor=border,
                    padding=(7, 8), font=("Segoe UI", 10, "bold"))
    style.map("Video.Treeview.Heading",
              background=[("active", colors["selection"])])
    style.configure("Vertical.TScrollbar", background=colors["header"],
                    troughcolor=panel, arrowcolor=colors["muted"])
    style.configure("Horizontal.TProgressbar", background=accent,
                    troughcolor=colors["border"], borderwidth=0, thickness=12)
    return colors
