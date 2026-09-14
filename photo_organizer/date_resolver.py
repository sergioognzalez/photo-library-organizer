from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import AppConfig
from .models import DateSource, MediaFile, MediaType

PHOTO_DATE_FIELDS = (
    "DateTimeOriginal",
    "SubSecDateTimeOriginal",
    "CreateDate",
    "MediaCreateDate",
    "CreationDate",
    "DateCreated",
)

VIDEO_DATE_FIELDS = (
    "MediaCreateDate",
    "TrackCreateDate",
    "CreateDate",
    "CreationDate",
    "DateTimeOriginal",
)

EXIF_DATE_RE = re.compile(
    r"(?P<year>19\d{2}|20\d{2})[:\-](?P<month>\d{2})[:\-](?P<day>\d{2})"
    r"(?:[ T](?P<hour>\d{2})[:.](?P<minute>\d{2})(?:[:.](?P<second>\d{2})(?:\.\d+)?)?)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)

FILENAME_DATE_PATTERNS = (
    re.compile(
        r"(?P<year>19\d{2}|20\d{2})(?P<month>\d{2})(?P<day>\d{2})"
        r"[_\-\s.]?(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})"
    ),
    re.compile(
        r"(?P<year>19\d{2}|20\d{2})[-_.](?P<month>\d{2})[-_.](?P<day>\d{2})"
        r"(?:[\s_,-]+(?P<hour>\d{2})[.\-_:](?P<minute>\d{2})(?:[.\-_:](?P<second>\d{2}))?)?"
    ),
    re.compile(
        r"(?P<year>19\d{2}|20\d{2})(?P<month>\d{2})(?P<day>\d{2})"
    ),
)


@dataclass(slots=True)
class DateResolution:
    value: datetime | None
    source: DateSource


def _valid_date(dt: datetime | None, config: AppConfig) -> datetime | None:
    if dt is None:
        return None
    if config.min_valid_year <= dt.year <= config.max_valid_year:
        return dt
    return None


def _timezone_from_suffix(value: str) -> ZoneInfo | None:
    if value.endswith("Z"):
        return ZoneInfo("UTC")
    return None


def parse_date_string(value: object, config: AppConfig) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("\u0000", "").strip()

    match = EXIF_DATE_RE.search(text)
    if match:
        parts = match.groupdict()
        try:
            second = int(parts.get("second") or 0)
            dt = datetime(
                int(parts["year"]),
                int(parts["month"]),
                int(parts["day"]),
                int(parts.get("hour") or 0),
                int(parts.get("minute") or 0),
                second,
            )
            tz = parts.get("tz")
            if tz:
                if tz == "Z":
                    dt = dt.replace(tzinfo=_timezone_from_suffix(tz))
                else:
                    normalized = tz if ":" in tz else f"{tz[:3]}:{tz[3:]}"
                    dt = datetime.fromisoformat(f"{dt.isoformat()}{normalized}")
            return _valid_date(dt, config)
        except ValueError:
            return None

    iso_candidate = text.replace(" ", "T")
    try:
        return _valid_date(datetime.fromisoformat(iso_candidate), config)
    except ValueError:
        return None


def parse_date_from_filename(path: Path, config: AppConfig) -> datetime | None:
    stem = path.stem
    for pattern in FILENAME_DATE_PATTERNS:
        match = pattern.search(stem)
        if not match:
            continue
        groups = match.groupdict()
        try:
            dt = datetime(
                int(groups["year"]),
                int(groups["month"]),
                int(groups["day"]),
                int(groups.get("hour") or 0),
                int(groups.get("minute") or 0),
                int(groups.get("second") or 0),
            )
        except ValueError:
            continue
        valid = _valid_date(dt, config)
        if valid is not None:
            return valid
    return None


def resolve_media_date(
    media_file: MediaFile,
    metadata: dict[str, object],
    config: AppConfig,
) -> DateResolution:
    fields = PHOTO_DATE_FIELDS if media_file.media_type == MediaType.PHOTO else VIDEO_DATE_FIELDS
    for field in fields:
        dt = parse_date_string(metadata.get(field), config)
        if dt is not None:
            return DateResolution(dt, DateSource.METADATA)

    dt = parse_date_from_filename(media_file.absolute_path, config)
    if dt is not None:
        return DateResolution(dt, DateSource.FILENAME)

    if config.use_filesystem_dates:
        try:
            fs_date = datetime.fromtimestamp(media_file.modified_time).astimezone()
            valid = _valid_date(fs_date, config)
            if valid is not None:
                return DateResolution(valid, DateSource.FILESYSTEM)
        except (OSError, ValueError):
            pass

    return DateResolution(None, DateSource.UNKNOWN)
