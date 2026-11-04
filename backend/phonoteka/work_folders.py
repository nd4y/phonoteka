"""User-selectable directories inside the installed Work mount."""
import os
from pathlib import Path

from . import media

EXCLUDED = {"@eadir", "#recycle", ".trash", ".snapshot", ".phonoteka-staging", "@recycle"}


def directory(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or len(relative) > 2048 or any(
        char == "\\" or ord(char) < 32 for char in relative
    ):
        raise media.MediaError("invalid_folder", "Некорректный путь к папке")
    current = root
    for part in relative.split("/") if relative else ():
        if part in {"", ".", ".."} or part.lower() in EXCLUDED:
            raise media.MediaError("invalid_folder", "Выберите папку внутри музыкального каталога")
        current /= part
        if current.is_symlink():
            raise media.MediaError("invalid_folder", "Ссылку нельзя выбрать как источник")
    if root.is_symlink() or not current.is_dir() or not current.resolve().is_relative_to(root.resolve()):
        raise media.MediaError("folder_unavailable", "Папка недоступна")
    return current


def browse(root: Path, relative: str, after: str):
    folder = directory(root, relative)
    try:
        with os.scandir(folder) as entries:
            names = sorted(entry.name for entry in entries if entry.name > after
                           and entry.name.lower() not in EXCLUDED and entry.is_dir(follow_symlinks=False)
                           and "\\" not in entry.name and all(ord(c) >= 32 for c in entry.name))
    except OSError as exc:
        raise media.MediaError("folder_unavailable", "Не удалось прочитать папку") from exc
    return {"folder": relative, "directories": names[:100], "next": names[99] if len(names) > 100 else None}
