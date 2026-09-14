from __future__ import annotations

import os
import stat
import threading
import time
from hashlib import sha256
from pathlib import Path

from .config import NON_COPYABLE_IGNORED_EXTENSIONS, NON_COPYABLE_IGNORED_NAMES
from .exceptions import OperationCancelled
from .file_types import classify_media, is_ignored_name
from .models import IgnoredFile, MediaFile, ScanResult

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4


class _ScanProgress:
    def __init__(self, callback):
        self.callback = callback
        self.directories = 0
        self.photos = 0
        self.videos = 0
        self.last_emit = 0.0

    def emit(
        self,
        result: ScanResult,
        current: Path,
        message: str | None = None,
        force: bool = False,
        ui_message: bool = False,
    ) -> None:
        if not self.callback:
            return
        now = time.monotonic()
        if not force and now - self.last_emit < 0.25 and result.total_seen % 25 != 0:
            return
        self.last_emit = now
        event = {
            "phase": "enumerando archivos",
            "current_file": str(current),
            "reviewed": result.total_seen,
            "compatible": len(result.files),
            "photos": self.photos,
            "videos": self.videos,
            "ignored": len(result.ignored),
            "errors": len(result.errors),
            "directories": self.directories,
            "progress_mode": "busy",
        }
        if message:
            event["message"] = message
            event["ui_message"] = ui_message
        self.callback(event)


def _is_reparse_point(entry: os.DirEntry[str]) -> bool:
    try:
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


def _is_hidden_or_system(entry: os.DirEntry[str]) -> bool:
    try:
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))


def _stable_id(path: Path) -> str:
    return sha256(str(path).lower().encode("utf-8", errors="replace")).hexdigest()[:24]


def _ignored_file(path: Path, source: Path, reason: str, size: int | None = None) -> IgnoredFile:
    try:
        relative_path = path.relative_to(source)
    except ValueError:
        relative_path = Path(path.name)
    extension = path.suffix
    name = path.name.lower()
    forced_noncopyable_reasons = (
        "enlace simbolico",
        "punto de union",
        "no es un archivo normal",
    )
    copyable = (
        not any(reason.startswith(prefix) for prefix in forced_noncopyable_reasons)
        and name not in NON_COPYABLE_IGNORED_NAMES
        and extension.lower() not in NON_COPYABLE_IGNORED_EXTENSIONS
    )
    return IgnoredFile(
        path=path,
        reason=reason,
        relative_path=relative_path,
        name=path.name,
        extension=extension,
        size=size,
        copyable=copyable,
    )


def _safe_size(path: Path) -> int | None:
    try:
        return path.lstat().st_size
    except OSError:
        return None


def scan_source(
    source: Path,
    cancel_event: threading.Event | None = None,
    progress_callback=None,
) -> ScanResult:
    result = ScanResult()
    stack = [source]
    progress = _ScanProgress(progress_callback)
    progress.emit(result, source, "Iniciando enumeracion recursiva", force=True, ui_message=True)

    while stack:
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled("Analisis cancelado durante el escaneo")

        current = stack.pop()
        progress.directories += 1
        progress.emit(result, current, f"Revisando carpeta: {current}", force=True)
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if cancel_event is not None and cancel_event.is_set():
                        raise OperationCancelled("Analisis cancelado durante el escaneo")

                    path = Path(entry.path)
                    result.total_seen += 1
                    progress.emit(result, path)

                    try:
                        if entry.is_symlink():
                            result.ignored.append(_ignored_file(path, source, "enlace simbolico ignorado", _safe_size(path)))
                            progress.emit(result, path, "Enlace simbolico ignorado", force=True)
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if _is_reparse_point(entry):
                                result.ignored.append(_ignored_file(path, source, "punto de union ignorado", None))
                                progress.emit(result, path, "Punto de union ignorado", force=True)
                                continue
                            stack.append(path)
                            progress.emit(result, path, f"Carpeta encontrada: {path}", force=True)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            result.ignored.append(_ignored_file(path, source, "no es un archivo normal", None))
                            progress.emit(result, path, "Entrada ignorada: no es un archivo normal", force=True)
                            continue
                    except OSError as exc:
                        result.errors.append((path, str(exc)))
                        progress.emit(result, path, f"Error revisando entrada: {exc}", force=True, ui_message=True)
                        continue

                    if is_ignored_name(path):
                        result.ignored.append(_ignored_file(path, source, "archivo auxiliar ignorado", _safe_size(path)))
                        progress.emit(result, path, "Archivo auxiliar ignorado", force=True)
                        continue

                    media_type = classify_media(path)
                    if media_type is None:
                        reason = "archivo oculto o de sistema ignorado" if _is_hidden_or_system(entry) else "extension no compatible"
                        result.ignored.append(_ignored_file(path, source, reason, _safe_size(path)))
                        progress.emit(result, path, reason, force=True)
                        continue

                    try:
                        progress.emit(result, path, f"Comprobando archivo compatible: {path}", force=True)
                        file_stat = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(file_stat.st_mode):
                            result.ignored.append(_ignored_file(path, source, "enlace simbolico ignorado", file_stat.st_size))
                            progress.emit(result, path, "Enlace simbolico ignorado", force=True)
                            continue
                        with path.open("rb"):
                            pass
                    except OSError as exc:
                        result.errors.append((path, str(exc)))
                        progress.emit(result, path, f"Error abriendo archivo: {exc}", force=True, ui_message=True)
                        continue

                    media_file = MediaFile(
                        id=_stable_id(path),
                        absolute_path=path,
                        relative_path=path.relative_to(source),
                        name=path.name,
                        extension=path.suffix,
                        media_type=media_type,
                        size=file_stat.st_size,
                        modified_time=file_stat.st_mtime,
                        analysis_status="scanned",
                    )
                    result.files.append(media_file)
                    if media_file.media_type.value == "foto":
                        progress.photos += 1
                    else:
                        progress.videos += 1

                    progress.emit(result, path, f"Archivo compatible detectado: {path}", force=True)
        except OSError as exc:
            result.errors.append((current, str(exc)))
            progress.emit(result, current, f"Error enumerando carpeta: {exc}", force=True, ui_message=True)

    result.files.sort(key=lambda item: str(item.relative_path).lower())
    progress.emit(result, source, "Enumeracion completada", force=True, ui_message=True)
    return result
