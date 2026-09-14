from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .models import DateSource, IgnoredFile, MediaFile, MediaType


class MediaDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS media_files (
                    id TEXT PRIMARY KEY,
                    absolute_path TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    modified_time REAL NOT NULL,
                    detected_date TEXT,
                    date_source TEXT NOT NULL,
                    hash TEXT,
                    duplicate_group TEXT,
                    analysis_status TEXT NOT NULL,
                    planned_destination TEXT,
                    copy_status TEXT NOT NULL,
                    verification_status TEXT NOT NULL,
                    error TEXT,
                    last_reviewed TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_identity ON media_files(absolute_path, size, modified_time)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ignored_files (
                    id TEXT PRIMARY KEY,
                    absolute_path TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    size INTEGER,
                    reason TEXT NOT NULL,
                    copyable INTEGER NOT NULL,
                    planned_destination TEXT,
                    copy_status TEXT NOT NULL,
                    verification_status TEXT NOT NULL,
                    error TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ignored_path ON ignored_files(absolute_path)"
            )
            connection.commit()

    def load_cached(self) -> dict[str, sqlite3.Row]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM media_files").fetchall()
        return {row["absolute_path"]: row for row in rows}

    def apply_cache(self, files: list[MediaFile]) -> None:
        cached = self.load_cached()
        for media_file in files:
            row = cached.get(str(media_file.absolute_path))
            if not row:
                continue
            if row["size"] != media_file.size or float(row["modified_time"]) != media_file.modified_time:
                continue
            if row["detected_date"]:
                try:
                    media_file.detected_date = datetime.fromisoformat(row["detected_date"])
                except ValueError:
                    media_file.detected_date = None
            media_file.date_source = DateSource(row["date_source"])
            media_file.hash = row["hash"]

    def save_files(self, files: list[MediaFile]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for media_file in files:
            rows.append(
                (
                    media_file.id,
                    str(media_file.absolute_path),
                    str(media_file.relative_path),
                    media_file.name,
                    media_file.extension,
                    media_file.media_type.value,
                    media_file.size,
                    media_file.modified_time,
                    media_file.detected_date.isoformat() if media_file.detected_date else None,
                    media_file.date_source.value,
                    media_file.hash,
                    media_file.duplicate_group,
                    media_file.analysis_status,
                    str(media_file.planned_destination) if media_file.planned_destination else None,
                    media_file.copy_status,
                    media_file.verification_status,
                    media_file.error,
                    media_file.last_reviewed.isoformat() if media_file.last_reviewed else None,
                    now,
                )
            )
        with closing(self._connect()) as connection:
            connection.executemany(
                """
                INSERT INTO media_files (
                    id, absolute_path, relative_path, name, extension, type, size,
                    modified_time, detected_date, date_source, hash, duplicate_group,
                    analysis_status, planned_destination, copy_status, verification_status,
                    error, last_reviewed, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    absolute_path=excluded.absolute_path,
                    relative_path=excluded.relative_path,
                    name=excluded.name,
                    extension=excluded.extension,
                    type=excluded.type,
                    size=excluded.size,
                    modified_time=excluded.modified_time,
                    detected_date=excluded.detected_date,
                    date_source=excluded.date_source,
                    hash=excluded.hash,
                    duplicate_group=excluded.duplicate_group,
                    analysis_status=excluded.analysis_status,
                    planned_destination=excluded.planned_destination,
                    copy_status=excluded.copy_status,
                    verification_status=excluded.verification_status,
                    error=excluded.error,
                    last_reviewed=excluded.last_reviewed,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
            connection.commit()

    def save_file(self, media_file: MediaFile) -> None:
        self.save_files([media_file])

    def load_files(self) -> list[MediaFile]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM media_files ORDER BY relative_path COLLATE NOCASE"
            ).fetchall()
        return [row_to_media_file(row) for row in rows]

    def save_ignored_files(self, ignored_files: list[IgnoredFile]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for ignored_file in ignored_files:
            rows.append(
                (
                    str(ignored_file.path).lower(),
                    str(ignored_file.path),
                    str(ignored_file.relative_path),
                    ignored_file.name,
                    ignored_file.extension,
                    ignored_file.size,
                    ignored_file.reason,
                    1 if ignored_file.copyable else 0,
                    str(ignored_file.planned_destination) if ignored_file.planned_destination else None,
                    ignored_file.copy_status,
                    ignored_file.verification_status,
                    ignored_file.error,
                    now,
                )
            )
        with closing(self._connect()) as connection:
            connection.executemany(
                """
                INSERT INTO ignored_files (
                    id, absolute_path, relative_path, name, extension, size, reason,
                    copyable, planned_destination, copy_status, verification_status, error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    absolute_path=excluded.absolute_path,
                    relative_path=excluded.relative_path,
                    name=excluded.name,
                    extension=excluded.extension,
                    size=excluded.size,
                    reason=excluded.reason,
                    copyable=excluded.copyable,
                    planned_destination=excluded.planned_destination,
                    copy_status=excluded.copy_status,
                    verification_status=excluded.verification_status,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
            connection.commit()

    def save_ignored_file(self, ignored_file: IgnoredFile) -> None:
        self.save_ignored_files([ignored_file])

    def load_ignored_files(self) -> list[IgnoredFile]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM ignored_files ORDER BY relative_path COLLATE NOCASE"
            ).fetchall()
        return [row_to_ignored_file(row) for row in rows]


def row_to_media_file(row: sqlite3.Row) -> MediaFile:
    detected_date = datetime.fromisoformat(row["detected_date"]) if row["detected_date"] else None
    return MediaFile(
        id=row["id"],
        absolute_path=Path(row["absolute_path"]),
        relative_path=Path(row["relative_path"]),
        name=row["name"],
        extension=row["extension"],
        media_type=MediaType(row["type"]),
        size=row["size"],
        modified_time=row["modified_time"],
        detected_date=detected_date,
        date_source=DateSource(row["date_source"]),
        hash=row["hash"],
        duplicate_group=row["duplicate_group"],
        analysis_status=row["analysis_status"],
        planned_destination=Path(row["planned_destination"]) if row["planned_destination"] else None,
        copy_status=row["copy_status"],
        verification_status=row["verification_status"],
        error=row["error"],
        last_reviewed=datetime.fromisoformat(row["last_reviewed"]) if row["last_reviewed"] else None,
    )


def row_to_ignored_file(row: sqlite3.Row) -> IgnoredFile:
    return IgnoredFile(
        path=Path(row["absolute_path"]),
        relative_path=Path(row["relative_path"]),
        name=row["name"],
        extension=row["extension"],
        size=row["size"],
        reason=row["reason"],
        copyable=bool(row["copyable"]),
        planned_destination=Path(row["planned_destination"]) if row["planned_destination"] else None,
        copy_status=row["copy_status"],
        verification_status=row["verification_status"],
        error=row["error"],
    )
