from __future__ import annotations

from pathlib import Path

from .config import IGNORED_EXTENSIONS, IGNORED_NAMES, PHOTO_EXTENSIONS, VIDEO_EXTENSIONS
from .models import MediaType


def normalized_extension(path: Path) -> str:
    return path.suffix.lower()


def is_ignored_name(path: Path) -> bool:
    name = path.name.lower()
    if name in IGNORED_NAMES:
        return True
    if name.startswith("~$"):
        return True
    if ".codexcopy-" in name and name.endswith(".tmp"):
        return True
    return normalized_extension(path) in IGNORED_EXTENSIONS


def classify_media(path: Path) -> MediaType | None:
    ext = normalized_extension(path)
    if ext in PHOTO_EXTENSIONS:
        return MediaType.PHOTO
    if ext in VIDEO_EXTENSIONS:
        return MediaType.VIDEO
    return None
