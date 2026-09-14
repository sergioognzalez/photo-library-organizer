from __future__ import annotations

import json
import os
import random
import shutil
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import (
    APP_DATA_DIR,
    COLLECTION_DIR,
    DUPLICATES_DIR,
    OTHER_FILES_DIR,
    PHOTO_EXTENSIONS,
    VIDEO_EXTENSIONS,
)
from .exceptions import OperationCancelled, ValidationError
from .file_types import classify_media
from .models import MediaType

REVIEW_TRASH_DIR = "PAPELERA_REVISION"
REVIEW_DATABASE_NAME = "seleccion_fotos.sqlite3"
REVIEW_ROOT_MARKERS = {COLLECTION_DIR, DUPLICATES_DIR, OTHER_FILES_DIR, REVIEW_TRASH_DIR}
AUXILIARY_EXTENSIONS = {".aae", ".xmp", ".thm", ".json", ".xml", ".dop"}


@dataclass(slots=True)
class ReviewFile:
    session_id: str
    original_path: Path
    current_path: Path
    relative_path: Path
    trash_relative_path: Path
    name: str
    extension: str
    media_type: str
    size: int
    modified_time: float
    position: int
    decision: str = "undecided"
    decision_at: str | None = None
    error: str | None = None


@dataclass(slots=True)
class ReviewSession:
    id: str
    album_root: Path
    review_root: Path
    trash_root: Path
    database_path: Path
    include_subfolders: bool
    media_filter: str
    ordering: str
    autoplay_videos: bool
    current_index: int
    files: list[ReviewFile] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    status: str = "active"

    @property
    def total_files(self) -> int:
        return len(self.files)


@dataclass(slots=True)
class ReviewSummary:
    total_files: int
    total_size: int
    total_size_gb: str
    kept: int
    discarded: int
    pending: int
    undecided: int
    trash_files: int
    trash_size: int
    trash_size_gb: str
    current_index: int


@dataclass(slots=True)
class MoveAudit:
    action_id: int
    source_path: Path
    destination_path: Path
    state: str
    error: str | None = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _format_gb(size: int) -> str:
    return f"{size / (1024 ** 3):.2f} GB"


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def detect_review_root(album_root: Path) -> Path:
    album_root = _resolve(album_root)
    if not album_root.exists() or not album_root.is_dir():
        raise ValidationError("La carpeta seleccionada no existe o no es una carpeta")
    for candidate in [album_root, *album_root.parents]:
        if candidate.name in REVIEW_ROOT_MARKERS:
            return candidate.parent.resolve(strict=False)
        marker_matches = [candidate / marker for marker in REVIEW_ROOT_MARKERS]
        if any(path.exists() and path.is_dir() for path in marker_matches):
            return candidate.resolve(strict=False)
    return album_root


def review_database_path(review_root: Path) -> Path:
    return review_root / APP_DATA_DIR / REVIEW_DATABASE_NAME


def _path_key(path: Path) -> str:
    return str(path.resolve(strict=False)).lower()


def _unique_destination(destination: Path) -> Path:
    candidate = destination
    counter = 2
    while candidate.exists():
        candidate = destination.with_name(f"{destination.stem}__{counter}{destination.suffix}")
        counter += 1
    return candidate


def _safe_relative(path: Path, root: Path) -> Path:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return Path(path.name)


def _trash_relative(path: Path, review_root: Path, album_root: Path) -> Path:
    if _is_relative_to(path, review_root):
        return _safe_relative(path, review_root)
    return Path(album_root.name) / _safe_relative(path, album_root)


def _enumerate_media(
    album_root: Path,
    review_root: Path,
    include_subfolders: bool,
    media_filter: str,
    cancel_event: threading.Event | None,
    progress_callback=None,
) -> list[Path]:
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled("Revision cancelada durante la enumeracion")

    media_filter = media_filter.lower()
    excluded_dirs = {REVIEW_TRASH_DIR.lower(), APP_DATA_DIR.lower()}
    candidates: list[Path] = []

    def include_path(path: Path) -> bool:
        media_type = classify_media(path)
        if media_type is None:
            return False
        if media_filter == "photos":
            return media_type == MediaType.PHOTO
        if media_filter == "videos":
            return media_type == MediaType.VIDEO
        return True

    if include_subfolders:
        for current, dirs, files in os.walk(album_root):
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelled("Revision cancelada durante la enumeracion")
            dirs[:] = [name for name in dirs if name.lower() not in excluded_dirs]
            current_path = Path(current)
            if _is_relative_to(current_path, review_root / REVIEW_TRASH_DIR):
                dirs[:] = []
                continue
            for name in files:
                path = current_path / name
                if include_path(path):
                    candidates.append(path)
                    if progress_callback and len(candidates) % 100 == 0:
                        progress_callback(
                            {
                                "phase": "seleccion: enumerando",
                                "current_file": str(path),
                                "reviewed": len(candidates),
                                "progress_mode": "busy",
                            }
                        )
    else:
        for path in album_root.iterdir():
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelled("Revision cancelada durante la enumeracion")
            if path.is_file() and include_path(path):
                candidates.append(path)
    return candidates


def _sort_paths(paths: list[Path], ordering: str) -> list[Path]:
    ordering = ordering.lower()
    if ordering == "name":
        return sorted(paths, key=lambda path: str(path.name).lower())
    if ordering == "date_desc":
        return sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)
    if ordering == "random":
        shuffled = list(paths)
        random.Random(0).shuffle(shuffled)
        return shuffled
    return sorted(paths, key=lambda path: path.stat().st_mtime)


def related_auxiliary_files(path: Path) -> list[Path]:
    stem = path.stem.lower()
    related: list[Path] = []
    try:
        for candidate in path.parent.iterdir():
            if not candidate.is_file() or candidate == path:
                continue
            if candidate.stem.lower() == stem and candidate.suffix.lower() in AUXILIARY_EXTENSIONS:
                related.append(candidate)
    except OSError:
        return []
    return sorted(related, key=lambda item: item.name.lower())


class ReviewDatabase:
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
                CREATE TABLE IF NOT EXISTS review_sessions (
                    id TEXT PRIMARY KEY,
                    album_root TEXT NOT NULL,
                    review_root TEXT NOT NULL,
                    trash_root TEXT NOT NULL,
                    include_subfolders INTEGER NOT NULL,
                    media_filter TEXT NOT NULL,
                    ordering TEXT NOT NULL,
                    autoplay_videos INTEGER NOT NULL,
                    current_index INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_files (
                    session_id TEXT NOT NULL,
                    original_path TEXT NOT NULL,
                    current_path TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    trash_relative_path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    modified_time REAL NOT NULL,
                    position INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    decision_at TEXT,
                    error TEXT,
                    PRIMARY KEY(session_id, original_path)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    file_original_path TEXT NOT NULL,
                    from_path TEXT NOT NULL,
                    to_path TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT,
                    details_json TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def save_session(self, session: ReviewSession) -> None:
        now = _now()
        if not session.created_at:
            session.created_at = now
        session.updated_at = now
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO review_sessions (
                    id, album_root, review_root, trash_root, include_subfolders,
                    media_filter, ordering, autoplay_videos, current_index,
                    created_at, updated_at, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    album_root=excluded.album_root,
                    review_root=excluded.review_root,
                    trash_root=excluded.trash_root,
                    include_subfolders=excluded.include_subfolders,
                    media_filter=excluded.media_filter,
                    ordering=excluded.ordering,
                    autoplay_videos=excluded.autoplay_videos,
                    current_index=excluded.current_index,
                    updated_at=excluded.updated_at,
                    status=excluded.status
                """,
                (
                    session.id,
                    str(session.album_root),
                    str(session.review_root),
                    str(session.trash_root),
                    1 if session.include_subfolders else 0,
                    session.media_filter,
                    session.ordering,
                    1 if session.autoplay_videos else 0,
                    session.current_index,
                    session.created_at,
                    session.updated_at,
                    session.status,
                ),
            )
            connection.commit()

    def save_files(self, files: list[ReviewFile]) -> None:
        with closing(self._connect()) as connection:
            connection.executemany(
                """
                INSERT INTO review_files (
                    session_id, original_path, current_path, relative_path,
                    trash_relative_path, name, extension, media_type, size,
                    modified_time, position, decision, decision_at, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, original_path) DO UPDATE SET
                    current_path=excluded.current_path,
                    relative_path=excluded.relative_path,
                    trash_relative_path=excluded.trash_relative_path,
                    name=excluded.name,
                    extension=excluded.extension,
                    media_type=excluded.media_type,
                    size=excluded.size,
                    modified_time=excluded.modified_time,
                    position=excluded.position,
                    decision=excluded.decision,
                    decision_at=excluded.decision_at,
                    error=excluded.error
                """,
                [
                    (
                        file.session_id,
                        str(file.original_path),
                        str(file.current_path),
                        str(file.relative_path),
                        str(file.trash_relative_path),
                        file.name,
                        file.extension,
                        file.media_type,
                        file.size,
                        file.modified_time,
                        file.position,
                        file.decision,
                        file.decision_at,
                        file.error,
                    )
                    for file in files
                ],
            )
            connection.commit()

    def save_file(self, file: ReviewFile) -> None:
        self.save_files([file])

    def load_session(self, session_id: str) -> ReviewSession:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM review_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ValidationError("No existe la sesion de revision")
            files = connection.execute(
                "SELECT * FROM review_files WHERE session_id = ? ORDER BY position",
                (session_id,),
            ).fetchall()
        return _session_from_row(row, [_file_from_row(file_row) for file_row in files], self.path)

    def latest_session_for_album(self, album_root: Path) -> ReviewSession | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM review_sessions
                WHERE album_root = ? AND status = 'active'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (str(album_root),),
            ).fetchone()
            if row is None:
                return None
            files = connection.execute(
                "SELECT * FROM review_files WHERE session_id = ? ORDER BY position",
                (row["id"],),
            ).fetchall()
        return _session_from_row(row, [_file_from_row(file_row) for file_row in files], self.path)

    def insert_action(
        self,
        session_id: str,
        action: str,
        file_original_path: Path,
        from_path: Path,
        to_path: Path | None,
        status: str,
        details: dict[str, Any],
    ) -> int:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO review_actions (
                    session_id, action, file_original_path, from_path, to_path,
                    status, created_at, details_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    action,
                    str(file_original_path),
                    str(from_path),
                    str(to_path) if to_path else None,
                    status,
                    _now(),
                    json.dumps(details, ensure_ascii=False),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def update_action(self, action_id: int, status: str, error: str | None = None) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE review_actions
                SET status = ?, completed_at = ?, error = ?
                WHERE id = ?
                """,
                (status, _now(), error, action_id),
            )
            connection.commit()

    def load_last_undoable_action(self, session_id: str) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            return connection.execute(
                """
                SELECT * FROM review_actions
                WHERE session_id = ? AND status = 'completed'
                ORDER BY id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()

    def incomplete_actions(self, session_id: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM review_actions WHERE status IN ('planned', 'moving')"
        params: tuple[Any, ...] = ()
        if session_id is not None:
            query += " AND session_id = ?"
            params = (session_id,)
        query += " ORDER BY id"
        with closing(self._connect()) as connection:
            return list(connection.execute(query, params).fetchall())

    def delete_completed_action(self, action_id: int) -> None:
        with closing(self._connect()) as connection:
            connection.execute("UPDATE review_actions SET status = 'undone' WHERE id = ?", (action_id,))
            connection.commit()


def _file_from_row(row: sqlite3.Row) -> ReviewFile:
    return ReviewFile(
        session_id=row["session_id"],
        original_path=Path(row["original_path"]),
        current_path=Path(row["current_path"]),
        relative_path=Path(row["relative_path"]),
        trash_relative_path=Path(row["trash_relative_path"]),
        name=row["name"],
        extension=row["extension"],
        media_type=row["media_type"],
        size=row["size"],
        modified_time=row["modified_time"],
        position=row["position"],
        decision=row["decision"],
        decision_at=row["decision_at"],
        error=row["error"],
    )


def _session_from_row(row: sqlite3.Row, files: list[ReviewFile], database_path: Path) -> ReviewSession:
    return ReviewSession(
        id=row["id"],
        album_root=Path(row["album_root"]),
        review_root=Path(row["review_root"]),
        trash_root=Path(row["trash_root"]),
        database_path=database_path,
        include_subfolders=bool(row["include_subfolders"]),
        media_filter=row["media_filter"],
        ordering=row["ordering"],
        autoplay_videos=bool(row["autoplay_videos"]),
        current_index=row["current_index"],
        files=files,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        status=row["status"],
    )


class PhotoSelectionService:
    def __init__(self, database_path_override: Path | None = None):
        self.database_path_override = database_path_override

    def _database(self, review_root: Path) -> ReviewDatabase:
        return ReviewDatabase(self.database_path_override or review_database_path(review_root))

    def prepare_album(
        self,
        album_root: Path | str,
        include_subfolders: bool = True,
        media_filter: str = "all",
        ordering: str = "date_asc",
        autoplay_videos: bool = False,
        cancel_event: threading.Event | None = None,
        progress_callback=None,
    ) -> ReviewSession:
        album_root = _resolve(Path(album_root))
        review_root = detect_review_root(album_root)
        trash_root = review_root / REVIEW_TRASH_DIR
        database = self._database(review_root)

        if progress_callback:
            progress_callback(
                {
                    "phase": "seleccion: enumerando",
                    "current_file": str(album_root),
                    "message": "Enumerando fotos y videos para revision",
                    "progress_mode": "busy",
                }
            )
        paths = _enumerate_media(
            album_root,
            review_root,
            include_subfolders,
            media_filter,
            cancel_event,
            progress_callback,
        )
        paths = _sort_paths(paths, ordering)
        session = ReviewSession(
            id=uuid.uuid4().hex,
            album_root=album_root,
            review_root=review_root,
            trash_root=trash_root,
            database_path=database.path,
            include_subfolders=include_subfolders,
            media_filter=media_filter,
            ordering=ordering,
            autoplay_videos=autoplay_videos,
            current_index=0,
        )
        files: list[ReviewFile] = []
        for index, path in enumerate(paths):
            stat = path.stat()
            media_type = classify_media(path)
            files.append(
                ReviewFile(
                    session_id=session.id,
                    original_path=path,
                    current_path=path,
                    relative_path=_safe_relative(path, album_root),
                    trash_relative_path=_trash_relative(path, review_root, album_root),
                    name=path.name,
                    extension=path.suffix,
                    media_type=media_type.value if media_type else "unknown",
                    size=stat.st_size,
                    modified_time=stat.st_mtime,
                    position=index,
                )
            )
        session.files = files
        database.save_session(session)
        database.save_files(files)
        return session

    def load_session(self, session: ReviewSession | str) -> ReviewSession:
        if isinstance(session, ReviewSession):
            database = self._database(session.review_root)
            return database.load_session(session.id)
        raise ValidationError("Se necesita una sesion con ruta de base de datos conocida")

    def latest_session_for_album(self, album_root: Path | str) -> ReviewSession | None:
        album_root = _resolve(Path(album_root))
        review_root = detect_review_root(album_root)
        return self._database(review_root).latest_session_for_album(album_root)

    def current_file(self, session: ReviewSession) -> ReviewFile | None:
        if not session.files:
            return None
        index = min(max(session.current_index, 0), len(session.files) - 1)
        return session.files[index]

    def next_index(self, session: ReviewSession, start: int) -> int:
        for index in range(start, len(session.files)):
            if session.files[index].decision == "undecided":
                return index
        return len(session.files)

    def previous_index(self, session: ReviewSession) -> int:
        return max(0, session.current_index - 1)

    def summary(self, session: ReviewSession) -> ReviewSummary:
        trash_files = self.trash_files(session.review_root)
        trash_size = sum(_safe_size(path) for path in trash_files)
        total_size = sum(file.size for file in session.files)
        return ReviewSummary(
            total_files=len(session.files),
            total_size=total_size,
            total_size_gb=_format_gb(total_size),
            kept=sum(1 for file in session.files if file.decision == "kept"),
            discarded=sum(1 for file in session.files if file.decision == "discarded"),
            pending=sum(1 for file in session.files if file.decision == "skipped"),
            undecided=sum(1 for file in session.files if file.decision == "undecided"),
            trash_files=len(trash_files),
            trash_size=trash_size,
            trash_size_gb=_format_gb(trash_size),
            current_index=session.current_index,
        )

    def keep_current(self, session: ReviewSession) -> ReviewSession:
        return self._mark_current(session, "kept", "keep")

    def skip_current(self, session: ReviewSession) -> ReviewSession:
        return self._mark_current(session, "skipped", "skip")

    def _mark_current(self, session: ReviewSession, decision: str, action: str) -> ReviewSession:
        file = self.current_file(session)
        if file is None:
            return session
        database = self._database(session.review_root)
        details = {"previous_decision": file.decision, "previous_current_path": str(file.current_path)}
        action_id = database.insert_action(
            session.id,
            action,
            file.original_path,
            file.current_path,
            file.current_path,
            "completed",
            details,
        )
        database.update_action(action_id, "completed")
        file.decision = decision
        file.decision_at = _now()
        file.error = None
        database.save_file(file)
        session.current_index = self.next_index(session, file.position + 1)
        database.save_session(session)
        return database.load_session(session.id)

    def discard_current(
        self,
        session: ReviewSession,
        move_auxiliaries: bool = True,
        simulate_interrupt_after_register: bool = False,
    ) -> ReviewSession:
        file = self.current_file(session)
        if file is None:
            return session
        database = self._database(session.review_root)
        if not file.current_path.exists():
            raise ValidationError(f"No existe el archivo a descartar: {file.current_path}")
        if not _is_relative_to(file.current_path, session.review_root) and not _is_relative_to(file.current_path, session.album_root):
            raise ValidationError("El archivo a descartar queda fuera de la carpeta revisada")

        target = _unique_destination(session.trash_root / file.trash_relative_path)
        auxiliary_paths = related_auxiliary_files(file.current_path) if move_auxiliaries else []
        auxiliary_moves = [
            {
                "from": str(aux_path),
                "to": str(_unique_destination(session.trash_root / _trash_relative(aux_path, session.review_root, session.album_root))),
            }
            for aux_path in auxiliary_paths
        ]
        details = {
            "previous_decision": file.decision,
            "previous_current_path": str(file.current_path),
            "auxiliary_moves": auxiliary_moves,
        }
        action_id = database.insert_action(
            session.id,
            "discard",
            file.original_path,
            file.current_path,
            target,
            "planned",
            details,
        )
        if simulate_interrupt_after_register:
            return database.load_session(session.id)

        try:
            database.update_action(action_id, "moving")
            _move_without_overwrite(file.current_path, target)
            completed_aux: list[dict[str, str]] = []
            for move in auxiliary_moves:
                from_path = Path(move["from"])
                to_path = Path(move["to"])
                if from_path.exists():
                    _move_without_overwrite(from_path, to_path)
                    completed_aux.append({"from": str(from_path), "to": str(to_path)})
            details["auxiliary_moves"] = completed_aux
            if not target.exists():
                raise ValidationError("El archivo no aparece en PAPELERA_REVISION tras moverlo")
            file.current_path = target
            file.decision = "discarded"
            file.decision_at = _now()
            file.error = None
            database.save_file(file)
            database.update_action(action_id, "completed")
        except Exception as exc:
            file.error = str(exc)
            database.save_file(file)
            database.update_action(action_id, "failed", str(exc))
            raise
        session.current_index = self.next_index(session, file.position + 1)
        database.save_session(session)
        return database.load_session(session.id)

    def undo_last(self, session: ReviewSession) -> ReviewSession:
        database = self._database(session.review_root)
        action = database.load_last_undoable_action(session.id)
        if action is None:
            return session
        details = json.loads(action["details_json"] or "{}")
        original_path = Path(action["file_original_path"])
        file = next((item for item in session.files if item.original_path == original_path), None)
        if file is None:
            return session

        if action["action"] == "discard":
            from_path = Path(action["to_path"])
            to_path = Path(action["from_path"])
            if not from_path.exists():
                raise ValidationError(f"No se puede deshacer: no existe {from_path}")
            if to_path.exists():
                raise ValidationError(f"No se puede deshacer sin sobrescribir: {to_path}")
            _move_without_overwrite(from_path, to_path)
            for move in reversed(details.get("auxiliary_moves", [])):
                aux_from = Path(move["to"])
                aux_to = Path(move["from"])
                if aux_from.exists():
                    if aux_to.exists():
                        raise ValidationError(f"No se puede restaurar auxiliar sin sobrescribir: {aux_to}")
                    _move_without_overwrite(aux_from, aux_to)
            file.current_path = to_path

        previous_decision = details.get("previous_decision", "undecided")
        file.decision = previous_decision
        file.decision_at = None if previous_decision == "undecided" else _now()
        file.error = None
        session.current_index = file.position
        database.save_file(file)
        database.save_session(session)
        database.delete_completed_action(action["id"])
        return database.load_session(session.id)

    def audit_incomplete_movements(self, session: ReviewSession) -> list[MoveAudit]:
        database = self._database(session.review_root)
        audits: list[MoveAudit] = []
        for action in database.incomplete_actions(session.id):
            source = Path(action["from_path"])
            destination = Path(action["to_path"]) if action["to_path"] else Path()
            source_exists = source.exists()
            destination_exists = destination.exists()
            if source_exists and not destination_exists:
                state = "en_origen"
                database.update_action(action["id"], "failed", "Movimiento registrado pero no ejecutado")
            elif not source_exists and destination_exists:
                state = "en_papelera"
                database.update_action(action["id"], "completed")
                file = next((item for item in session.files if item.original_path == Path(action["file_original_path"])), None)
                if file is not None:
                    file.current_path = destination
                    file.decision = "discarded"
                    file.decision_at = _now()
                    database.save_file(file)
            elif source_exists and destination_exists:
                state = "en_ambas"
                database.update_action(action["id"], "failed", "El archivo aparece en origen y papelera")
            else:
                state = "en_ninguna"
                database.update_action(action["id"], "failed", "El archivo no aparece ni en origen ni en papelera")
            audits.append(MoveAudit(action["id"], source, destination, state))
        return audits

    def trash_files(self, review_root: Path | str) -> list[Path]:
        trash_root = _resolve(Path(review_root)) / REVIEW_TRASH_DIR
        if not trash_root.exists():
            return []
        return sorted((path for path in trash_root.rglob("*") if path.is_file()), key=lambda path: str(path).lower())

    def restore_trash_file(self, review_root: Path | str, trash_file: Path | str) -> Path:
        review_root = _resolve(Path(review_root))
        trash_root = review_root / REVIEW_TRASH_DIR
        trash_file = _resolve(Path(trash_file))
        if not _is_relative_to(trash_file, trash_root):
            raise ValidationError("El archivo no esta dentro de PAPELERA_REVISION")
        relative = trash_file.relative_to(trash_root)
        original = review_root / relative
        if original.exists():
            raise ValidationError(f"No se restaura sin sobrescribir: {original}")
        _move_without_overwrite(trash_file, original)
        return original

    def restore_all_trash(self, review_root: Path | str) -> list[Path]:
        restored: list[Path] = []
        for trash_file in self.trash_files(Path(review_root)):
            restored.append(self.restore_trash_file(review_root, trash_file))
        return restored

    def can_permanently_delete_trash(self, checkbox_checked: bool, typed_text: str, second_confirmed: bool) -> bool:
        return checkbox_checked and typed_text == "ELIMINAR" and second_confirmed

    def permanently_delete_trash(
        self,
        review_root: Path | str,
        checkbox_checked: bool,
        typed_text: str,
        second_confirmed: bool,
    ) -> int:
        if not self.can_permanently_delete_trash(checkbox_checked, typed_text, second_confirmed):
            raise ValidationError("Confirmacion insuficiente para eliminar definitivamente")
        deleted = 0
        for trash_file in self.trash_files(Path(review_root)):
            trash_file.unlink()
            deleted += 1
        return deleted


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _move_without_overwrite(source: Path, destination: Path) -> None:
    if not source.exists():
        raise ValidationError(f"No existe el origen del movimiento: {source}")
    if destination.exists():
        raise ValidationError(f"No se sobrescribe el destino: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    if not destination.exists():
        raise ValidationError(f"Movimiento no confirmado en destino: {destination}")
