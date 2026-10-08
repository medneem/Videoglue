"""Include tkDnD's Tcl scripts and native libraries in the executable."""

from PyInstaller.utils.hooks import collect_data_files


datas = collect_data_files("tkinterdnd2", includes=["tkdnd/**"])
