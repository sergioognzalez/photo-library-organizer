from __future__ import annotations

import re
from pathlib import Path

from .config import COLLECTION_DIR, DUPLICATES_DIR, MONTH_NAMES
from .config import OTHER_FILES_DIR
from .models import DateSource, IgnoredFile, MediaFile, MediaType

WINDOWS_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE = re.compile(r"\s+")


def sanitize_part(value: str, fallback: str = "archivo", compact_whitespace: bool = False) -> str:
    cleaned = WINDOWS_FORBIDDEN.sub("_", value)
    replacement = "_" if compact_whitespace else " "
    cleaned = WHITESPACE.sub(replacement, cleaned).strip(" ._")
    if not cleaned:
        cleaned = fallback
    return cleaned[:90]


def _with_counter(path: Path, reserved: set[Path]) -> Path:
    candidate = path
    counter = 2
    while candidate.exists() or candidate in reserved:
        candidate = path.with_name(f"{path.stem}__{counter}{path.suffix}")
        counter += 1
    reserved.add(candidate)
    return candidate


def _unique_destination(media_file: MediaFile, destination: Path, reserved: set[Path]) -> Path:
    if media_file.detected_date is None or media_file.date_source == DateSource.UNKNOWN:
        base = destination / COLLECTION_DIR / "SIN_FECHA"
    else:
        base = (
            destination
            / COLLECTION_DIR
            / f"{media_file.detected_date.year:04d}"
            / MONTH_NAMES[media_file.detected_date.month]
        )

    type_dir = "Fotos" if media_file.media_type == MediaType.PHOTO else "Videos"
    return _with_counter(base / type_dir / sanitize_part(media_file.name), reserved)


def _duplicate_destination(media_file: MediaFile, destination: Path, reserved: set[Path]) -> Path:
    group = media_file.duplicate_group or "Duplicado sin grupo"
    parent_parts = [
        sanitize_part(part, "origen", compact_whitespace=True)
        for part in media_file.relative_path.parts[:-1]
    ]
    origin_prefix = "_".join(parent_parts)
    filename = sanitize_part(media_file.name)
    if origin_prefix:
        filename = f"{origin_prefix}__{filename}"
    return _with_counter(destination / DUPLICATES_DIR / group / filename, reserved)


def assign_destinations(files: list[MediaFile], destination: Path) -> list[MediaFile]:
    reserved: set[Path] = set()
    for media_file in sorted(files, key=lambda item: str(item.relative_path).lower()):
        if media_file.analysis_status == "error":
            continue
        if media_file.is_duplicate:
            media_file.planned_destination = _duplicate_destination(media_file, destination, reserved)
        else:
            media_file.planned_destination = _unique_destination(media_file, destination, reserved)
    return files


def assign_ignored_destinations(
    ignored: list[IgnoredFile],
    destination: Path,
    copy_ignored_files: bool,
) -> list[IgnoredFile]:
    reserved: set[Path] = set()
    for ignored_file in sorted(ignored, key=lambda item: str(item.relative_path).lower()):
        if not copy_ignored_files or not ignored_file.copyable:
            ignored_file.planned_destination = None
            ignored_file.copy_status = "not_applicable"
            ignored_file.verification_status = "not_applicable"
            continue
        safe_parts = [
            sanitize_part(part, fallback="archivo", compact_whitespace=False)
            for part in ignored_file.relative_path.parts
        ]
        ignored_file.planned_destination = _with_counter(
            destination.joinpath(OTHER_FILES_DIR, *safe_parts),
            reserved,
        )
    return ignored
