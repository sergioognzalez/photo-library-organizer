from __future__ import annotations

import csv
import hashlib
import json
import shutil
import struct
import subprocess
import threading
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import (
    APP_DATA_DIR,
    COLLECTION_DIR,
    DUPLICATES_DIR,
    OTHER_FILES_DIR,
    REPORTS_DIR,
    AppConfig,
)
from .exceptions import OperationCancelled, ValidationError
from .file_types import classify_media
from .models import (
    AnalysisResult,
    FinalVerificationResult,
    FinalVerificationRow,
    IgnoredFile,
    MediaFile,
    MultimediaVerificationResult,
    MultimediaVerificationRow,
)
from .resume import load_previous_analysis
from .scanner import scan_source


FINAL_CSV = "verificacion_final_completa.csv"
FINAL_JSON = "verificacion_final_completa.json"
FINAL_LOG = "verificacion_final_tecnica.log"
MULTIMEDIA_CSV = "verificacion_multimedia.csv"
MULTIMEDIA_JSON = "verificacion_multimedia.json"
MULTIMEDIA_LOG = "verificacion_multimedia_tecnica.log"


def _format_gb(size: int) -> str:
    return f"{size / (1024 ** 3):.2f} GB"


def _add_size(summary: dict[str, Any], key: str, value: int) -> None:
    summary[key] = value
    summary[f"{key}_gb"] = _format_gb(value)


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _safe_resolve(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


class _TechnicalLog:
    def __init__(self, destination: Path, filename: str, title: str):
        reports_dir = destination / REPORTS_DIR
        reports_dir.mkdir(parents=True, exist_ok=True)
        self.path = reports_dir / filename
        self.path.write_text(f"{title}\n", encoding="utf-8")

    def write(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload["timestamp"] = datetime.now().isoformat(timespec="seconds")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str))
            handle.write("\n")


class _PlannedEntry:
    def __init__(
        self,
        source_path: Path,
        relative_path: Path,
        destination_path: Path | None,
        expected_zone: str | None,
        copyable: bool,
    ):
        self.source_path = source_path
        self.relative_path = relative_path
        self.destination_path = destination_path
        self.expected_zone = expected_zone
        self.copyable = copyable


def _validate_read_only_paths(source: Path, destination: Path) -> tuple[Path, Path]:
    source = _safe_resolve(source)
    destination = _safe_resolve(destination)
    if not source.exists() or not source.is_dir():
        raise ValidationError("La carpeta de origen no existe o no es una carpeta")
    if not destination.exists() or not destination.is_dir():
        raise ValidationError("La carpeta de destino no existe o no es una carpeta")
    if source == destination:
        raise ValidationError("Origen y destino no pueden ser la misma carpeta")
    if _is_relative_to(destination, source):
        raise ValidationError("El destino no puede estar dentro del origen")
    if _is_relative_to(source, destination):
        raise ValidationError("El origen no puede estar dentro del destino")
    return source, destination


def _relative_key(path: Path) -> str:
    return str(path).replace("/", "\\").lower()


def _plan_entries(analysis: AnalysisResult) -> list[_PlannedEntry]:
    entries: list[_PlannedEntry] = []
    for media_file in analysis.files:
        if media_file.analysis_status == "error":
            continue
        expected_zone = DUPLICATES_DIR if media_file.is_duplicate else COLLECTION_DIR
        entries.append(
            _PlannedEntry(
                media_file.absolute_path,
                media_file.relative_path,
                media_file.planned_destination,
                expected_zone,
                True,
            )
        )
    for ignored_file in analysis.ignored:
        expected_zone = OTHER_FILES_DIR if ignored_file.copyable else None
        entries.append(
            _PlannedEntry(
                ignored_file.path,
                ignored_file.relative_path,
                ignored_file.planned_destination,
                expected_zone,
                ignored_file.copyable,
            )
        )
    return entries


def _current_source_files(scan_files: list[MediaFile], ignored: list[IgnoredFile]) -> list[tuple[Path, Path, int | None]]:
    current: list[tuple[Path, Path, int | None]] = []
    for media_file in scan_files:
        current.append((media_file.absolute_path, media_file.relative_path, media_file.size))
    for ignored_file in ignored:
        current.append((ignored_file.path, ignored_file.relative_path, ignored_file.size))
    return sorted(current, key=lambda item: _relative_key(item[1]))


def _duplicate_destinations(entries: list[_PlannedEntry]) -> list[Path]:
    seen: dict[Path, int] = {}
    for entry in entries:
        if entry.destination_path is None:
            continue
        resolved = entry.destination_path.resolve(strict=False)
        seen[resolved] = seen.get(resolved, 0) + 1
    return sorted([path for path, count in seen.items() if count > 1], key=lambda path: str(path).lower())


def _zone_is_valid(destination_path: Path, destination_root: Path, expected_zone: str | None) -> bool:
    if expected_zone is None:
        return False
    return _is_relative_to(destination_path, destination_root / expected_zone)


def _hash_and_size(
    path: Path,
    config: AppConfig,
    cancel_event: threading.Event | None,
) -> tuple[int | None, str | None, str | None]:
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled("Verificacion final cancelada")
    try:
        if path.is_symlink() or not path.is_file():
            return None, None, "La ruta no es un archivo normal"
        size = path.stat().st_size
        hasher = hashlib.new(config.hash_algorithm)
        with path.open("rb") as handle:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise OperationCancelled("Verificacion final cancelada")
                block = handle.read(config.normalized_block_size())
                if not block:
                    break
                hasher.update(block)
        return size, hasher.hexdigest(), None
    except OperationCancelled:
        raise
    except OSError as exc:
        return None, None, str(exc)


def _scan_destination_zone_files(destination: Path) -> list[Path]:
    roots = [destination / COLLECTION_DIR, destination / DUPLICATES_DIR, destination / OTHER_FILES_DIR]
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        try:
            files.extend(path for path in root.rglob("*") if path.is_file())
        except OSError:
            continue
    return sorted(files, key=lambda path: str(path).lower())


def _is_temp_copy(path: Path) -> bool:
    name = path.name.lower()
    return ".codexcopy-" in name and name.endswith(".tmp")


def _is_internal_destination_file(path: Path) -> bool:
    return path.name.lower() == "origenes.json"


def _write_final_reports(
    destination: Path,
    rows: list[FinalVerificationRow],
    summary: dict[str, Any],
    temp_files: list[Path],
    unplanned_files: list[Path],
    unexpected_duplicates: list[Path],
    duplicate_planned_destinations: list[Path],
    planned_missing_from_source: list[Path],
    log_path: Path,
) -> dict[str, Path]:
    reports_dir = destination / REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    csv_path = reports_dir / FINAL_CSV
    json_path = reports_dir / FINAL_JSON
    fieldnames = [
        "ruta_origen",
        "ruta_destino",
        "tamaño_origen",
        "tamaño_destino",
        "hash_origen",
        "hash_destino",
        "resultado",
        "motivo_error",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_csv_dict())

    payload = {
        "generado_en": datetime.now().isoformat(timespec="seconds"),
        "resumen": summary,
        "filas": [row.to_csv_dict() for row in rows],
        "temporales": [str(path) for path in temp_files],
        "destinos_no_planificados": [str(path) for path in unplanned_files],
        "duplicados_inesperados": [str(path) for path in unexpected_duplicates],
        "destinos_planificados_duplicados": [str(path) for path in duplicate_planned_destinations],
        "planificados_no_en_origen": [str(path) for path in planned_missing_from_source],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"csv": csv_path, "json": json_path, "log": log_path}


def run_final_verification(
    source: Path | str,
    destination: Path | str,
    config: AppConfig,
    cancel_event: threading.Event | None = None,
    progress_callback=None,
) -> FinalVerificationResult:
    source_path, destination_path = _validate_read_only_paths(Path(source), Path(destination))
    log = _TechnicalLog(destination_path, FINAL_LOG, "Verificacion final completa")
    log.write({"phase": "inicio", "source": str(source_path), "destination": str(destination_path)})
    if progress_callback:
        progress_callback(
            {
                "phase": "verificacion final: leyendo plan",
                "current_file": str(destination_path),
                "message": "Leyendo plan anterior. Los estados guardados se ignoraran.",
                "log_path": str(log.path),
                "progress_mode": "busy",
            }
        )

    analysis, _, _ = load_previous_analysis(destination_path, source_hint=source_path)
    entries = _plan_entries(analysis)
    plan_by_relative: dict[str, _PlannedEntry] = {}
    duplicate_source_plan: list[Path] = []
    for entry in entries:
        key = _relative_key(entry.relative_path)
        if key in plan_by_relative:
            duplicate_source_plan.append(entry.relative_path)
        else:
            plan_by_relative[key] = entry
    duplicate_destination_paths = _duplicate_destinations(entries)

    if progress_callback:
        progress_callback(
            {
                "phase": "verificacion final: enumerando origen",
                "current_file": str(source_path),
                "message": "Enumerando de nuevo todos los archivos del origen.",
                "progress_mode": "busy",
            }
        )
    scan_result = scan_source(source_path, cancel_event, progress_callback)
    current_files = _current_source_files(scan_result.files, scan_result.ignored)
    current_keys = {_relative_key(relative_path) for _, relative_path, _ in current_files}
    planned_missing_from_source = [
        entry.relative_path
        for entry in entries
        if _relative_key(entry.relative_path) not in current_keys
    ]

    rows: list[FinalVerificationRow] = []
    planned_destination_hashes: dict[tuple[int, str], list[Path]] = {}
    total = len(current_files)
    for index, (source_file, relative_path, scanned_size) in enumerate(current_files, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled("Verificacion final cancelada")
        if progress_callback:
            progress_callback(
                {
                    "phase": "verificacion final: comparando hashes",
                    "current_file": str(source_file),
                    "current": index,
                    "total": total,
                    "reviewed": index,
                    "progress_mode": "percent",
                    "message": f"Verificando byte a byte: {source_file}",
                    "ui_message": False,
                }
            )

        source_size, source_hash, source_error = _hash_and_size(source_file, config, cancel_event)
        entry = plan_by_relative.get(_relative_key(relative_path))
        destination_file = entry.destination_path if entry else None
        destination_size: int | None = None
        destination_hash: str | None = None
        destination_error: str | None = None
        result = "correcto"
        reason: str | None = None

        if source_error is not None:
            result = "error_lectura_origen"
            reason = source_error
        elif entry is None:
            result = "sin_planificar"
            reason = "El archivo existe en el origen actual pero no aparece en el plan anterior"
        elif not entry.copyable or destination_file is None:
            result = "sin_planificar"
            reason = "El plan anterior no contiene destino previsto para este archivo"
        elif not _zone_is_valid(destination_file, destination_path, entry.expected_zone):
            result = "destino_fuera_de_zona"
            reason = f"Destino fuera de la zona esperada: {entry.expected_zone}"
        elif not destination_file.exists():
            result = "ausente"
            reason = "El destino planificado no existe"
        else:
            destination_size, destination_hash, destination_error = _hash_and_size(
                destination_file,
                config,
                cancel_event,
            )
            if destination_error is not None:
                result = "error_lectura_destino"
                reason = destination_error
            elif source_size != destination_size:
                result = "tamaño_diferente"
                reason = f"Origen={source_size}, destino={destination_size}"
            elif source_hash != destination_hash:
                result = "hash_diferente"
                reason = "Mismo tamaño pero hash distinto"
            else:
                result = "correcto"
                reason = None
                if source_size is not None and source_hash is not None:
                    planned_destination_hashes.setdefault((source_size, source_hash), []).append(destination_file)

        if destination_file is not None and destination_size is None and destination_file.exists():
            try:
                destination_size = destination_file.stat().st_size
            except OSError:
                pass
        row = FinalVerificationRow(
            source_path=source_file,
            destination_path=destination_file,
            source_size=source_size if source_size is not None else scanned_size,
            destination_size=destination_size,
            source_hash=source_hash,
            destination_hash=destination_hash,
            result=result,
            error_reason=reason,
        )
        rows.append(row)
        log.write(row.to_csv_dict())

    all_destination_files = _scan_destination_zone_files(destination_path)
    planned_destinations = {
        entry.destination_path.resolve(strict=False)
        for entry in entries
        if entry.destination_path is not None
    }
    temp_files = [path for path in all_destination_files if _is_temp_copy(path)]
    unplanned_destination_files = [
        path
        for path in all_destination_files
        if path.resolve(strict=False) not in planned_destinations
        and not _is_temp_copy(path)
        and not _is_internal_destination_file(path)
    ]

    unexpected_duplicates: list[Path] = []
    for path in unplanned_destination_files:
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled("Verificacion final cancelada")
        size, file_hash, error = _hash_and_size(path, config, cancel_event)
        if error is None and size is not None and file_hash is not None:
            if (size, file_hash) in planned_destination_hashes:
                unexpected_duplicates.append(path)

    originals_found = len(current_files)
    located = sum(1 for row in rows if row.destination_path is not None and row.destination_path.exists())
    identical = sum(1 for row in rows if row.result == "correcto")
    missing = sum(1 for row in rows if row.result == "ausente")
    size_different = sum(1 for row in rows if row.result == "tamaño_diferente")
    hash_different = sum(1 for row in rows if row.result == "hash_diferente")
    unplanned = sum(1 for row in rows if row.result in {"sin_planificar", "destino_fuera_de_zona"})
    read_errors = sum(
        1
        for row in rows
        if row.result in {"error_lectura_origen", "error_lectura_destino"}
    ) + len(scan_result.errors)
    bytes_originals = sum(row.source_size or 0 for row in rows)
    bytes_verified = sum(row.destination_size or 0 for row in rows if row.result == "correcto")
    bytes_planned_destinations = sum(row.destination_size or 0 for row in rows if row.destination_path is not None)

    summary: dict[str, Any] = {
        "estado": "NO ES SEGURO BORRAR EL ORIGEN",
        "archivos_originales_encontrados": originals_found,
        "archivos_planificados": len(entries),
        "archivos_con_destino_localizado": located,
        "archivos_identicos_por_hash": identical,
        "archivos_ausentes": missing,
        "tamaños_diferentes": size_different,
        "hashes_diferentes": hash_different,
        "archivos_sin_planificar": unplanned,
        "temporales": len(temp_files),
        "errores_lectura": read_errors,
        "archivos_destino_no_contemplados": len(unplanned_destination_files),
        "archivos_duplicados_inesperados": len(unexpected_duplicates),
        "destinos_planificados_duplicados": len(duplicate_destination_paths),
        "origenes_planificados_duplicados": len(duplicate_source_plan),
        "planificados_no_en_origen": len(planned_missing_from_source),
        "cancelado": False,
    }
    _add_size(summary, "bytes_originales", bytes_originals)
    _add_size(summary, "bytes_verificados_destino", bytes_verified)
    _add_size(summary, "bytes_destinos_planificados", bytes_planned_destinations)
    summary["suma_tamaños_original_destino_coincide"] = bytes_originals == bytes_verified

    passed = (
        originals_found > 0
        and originals_found == located == identical
        and missing == 0
        and size_different == 0
        and hash_different == 0
        and unplanned == 0
        and len(temp_files) == 0
        and read_errors == 0
        and len(unplanned_destination_files) == 0
        and len(unexpected_duplicates) == 0
        and len(duplicate_destination_paths) == 0
        and len(duplicate_source_plan) == 0
        and len(planned_missing_from_source) == 0
        and bytes_originals == bytes_verified
    )
    if passed:
        summary["estado"] = "VERIFICACIÓN FINAL SUPERADA"
    summary["verificacion_final_superada"] = passed
    log.write({"phase": "resumen", **summary})
    report_paths = _write_final_reports(
        destination_path,
        rows,
        summary,
        temp_files,
        unplanned_destination_files,
        unexpected_duplicates,
        duplicate_destination_paths,
        planned_missing_from_source,
        log.path,
    )
    if progress_callback:
        progress_callback(
            {
                "phase": "verificacion final completada",
                "current": 1,
                "total": 1,
                "progress_mode": "percent",
                "message": summary["estado"],
            }
        )
    return FinalVerificationResult(
        source=source_path,
        destination=destination_path,
        rows=rows,
        summary=summary,
        report_paths=report_paths,
        temp_files=temp_files,
        unplanned_destination_files=unplanned_destination_files,
        unexpected_duplicate_files=unexpected_duplicates,
        duplicate_planned_destinations=duplicate_destination_paths,
        planned_missing_from_source=planned_missing_from_source,
    )


def _validate_png(data: bytes) -> str | None:
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        return "Firma PNG incorrecta"
    offset = len(signature)
    seen_iend = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_data_start = offset + 8
        chunk_data_end = chunk_data_start + length
        crc_end = chunk_data_end + 4
        if crc_end > len(data):
            return "Chunk PNG truncado"
        expected_crc = struct.unpack(">I", data[chunk_data_end:crc_end])[0]
        actual_crc = zlib.crc32(chunk_type + data[chunk_data_start:chunk_data_end]) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            return "CRC PNG incorrecto"
        offset = crc_end
        if chunk_type == b"IEND":
            seen_iend = True
            break
    if not seen_iend:
        return "PNG sin IEND"
    if offset != len(data):
        return "PNG con datos extra al final"
    return None


def _validate_photo_basic(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return str(exc)
    if not data:
        return "Archivo vacio"
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
            return "JPEG sin marcador de inicio o fin"
        if b"\xff\xda" not in data:
            return "JPEG sin segmento de imagen"
        return None
    if ext == ".png":
        return _validate_png(data)
    if ext == ".gif":
        if not (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
            return "Firma GIF incorrecta"
        if data[-1:] != b"\x3b":
            return "GIF sin trailer final"
        return None
    if ext == ".bmp":
        if not data.startswith(b"BM") or len(data) < 14:
            return "Firma BMP incorrecta"
        declared_size = struct.unpack("<I", data[2:6])[0]
        if declared_size != len(data):
            return "Tamaño BMP declarado no coincide"
        return None
    if ext in {".tif", ".tiff", ".dng", ".cr2"}:
        if not (data.startswith(b"II*\x00") or data.startswith(b"MM\x00*")):
            return "Cabecera TIFF/RAW no reconocida"
        return None
    if ext in {".heic", ".heif", ".avif"}:
        if len(data) < 16 or data[4:8] != b"ftyp":
            return "Contenedor HEIF/AVIF sin caja ftyp"
        return None
    if ext == ".webp":
        if len(data) < 12 or not data.startswith(b"RIFF") or data[8:12] != b"WEBP":
            return "Firma WEBP incorrecta"
        return None
    return "Validador de estructura no disponible para esta extension"


def _validate_photo(path: Path) -> str | None:
    try:
        from PySide6.QtGui import QImageReader

        reader = QImageReader(str(path))
        reader.setDecideFormatFromContent(True)
        image = reader.read()
        if not image.isNull():
            return None
        qt_error = reader.errorString()
    except Exception as exc:
        qt_error = str(exc)
    basic_error = _validate_photo_basic(path)
    if basic_error is None:
        return None
    return f"{basic_error}; lector de imagen: {qt_error}"


def _video_probe_tool() -> tuple[str | None, list[str]]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg, [ffmpeg, "-v", "error", "-i"]
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        return ffprobe, [ffprobe, "-v", "error", "-show_format", "-show_streams"]
    return None, []


def _validate_video(path: Path, timeout_seconds: int = 120) -> str | None:
    tool, base_command = _video_probe_tool()
    if tool is None:
        return "ffprobe/ffmpeg no encontrado"
    if Path(tool).name.lower().startswith("ffprobe"):
        command = [*base_command, str(path)]
    else:
        command = [*base_command, str(path), "-f", "null", "-"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"Tiempo agotado tras {timeout_seconds}s"
    except OSError as exc:
        return str(exc)
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "").strip()
        return details[:1000] or f"La herramienta termino con codigo {completed.returncode}"
    return None


def _write_multimedia_reports(
    destination: Path,
    rows: list[MultimediaVerificationRow],
    summary: dict[str, Any],
    log_path: Path,
) -> dict[str, Path]:
    reports_dir = destination / REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    csv_path = reports_dir / MULTIMEDIA_CSV
    json_path = reports_dir / MULTIMEDIA_JSON
    fieldnames = ["ruta", "tipo", "resultado", "motivo_error"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_csv_dict())
    payload = {
        "generado_en": datetime.now().isoformat(timespec="seconds"),
        "resumen": summary,
        "filas": [row.to_csv_dict() for row in rows],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"csv": csv_path, "json": json_path, "log": log_path}


def run_multimedia_verification(
    source: Path | str | None,
    destination: Path | str,
    config: AppConfig,
    cancel_event: threading.Event | None = None,
    progress_callback=None,
) -> MultimediaVerificationResult:
    destination_path = _safe_resolve(Path(destination))
    if not destination_path.exists() or not destination_path.is_dir():
        raise ValidationError("La carpeta de destino no existe o no es una carpeta")
    source_hint = _safe_resolve(Path(source)) if source else None
    log = _TechnicalLog(destination_path, MULTIMEDIA_LOG, "Verificacion multimedia")
    analysis, _, _ = load_previous_analysis(destination_path, source_hint=source_hint)
    media_files = [
        media_file
        for media_file in analysis.files
        if media_file.analysis_status != "error" and media_file.planned_destination is not None
    ]
    rows: list[MultimediaVerificationRow] = []
    total = len(media_files)
    for index, media_file in enumerate(media_files, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled("Verificacion multimedia cancelada")
        destination_file = media_file.planned_destination
        if progress_callback:
            progress_callback(
                {
                    "phase": "verificacion multimedia",
                    "current_file": str(destination_file),
                    "current": index,
                    "total": total,
                    "progress_mode": "percent",
                    "message": f"Validando multimedia: {destination_file}",
                    "ui_message": False,
                }
            )
        if not destination_file.exists():
            row = MultimediaVerificationRow(destination_file, media_file.media_type.value, "error", "Destino no existe")
        elif classify_media(destination_file) is None:
            row = MultimediaVerificationRow(destination_file, media_file.media_type.value, "error", "Extension no compatible")
        elif media_file.media_type.value == "foto":
            error = _validate_photo(destination_file)
            row = MultimediaVerificationRow(destination_file, "foto", "correcto" if error is None else "error", error)
        else:
            error = _validate_video(destination_file)
            row = MultimediaVerificationRow(destination_file, "video", "correcto" if error is None else "error", error)
        rows.append(row)
        log.write(row.to_csv_dict())

    errors = sum(1 for row in rows if row.result != "correcto")
    summary: dict[str, Any] = {
        "estado": "VERIFICACION MULTIMEDIA SUPERADA" if errors == 0 else "ERRORES EN VERIFICACION MULTIMEDIA",
        "archivos_multimedia_revisados": len(rows),
        "fotografias_revisadas": sum(1 for row in rows if row.media_type == "foto"),
        "videos_revisados": sum(1 for row in rows if row.media_type == "video"),
        "correctos": sum(1 for row in rows if row.result == "correcto"),
        "errores": errors,
        "ffprobe_disponible": shutil.which("ffprobe") is not None,
        "ffmpeg_disponible": shutil.which("ffmpeg") is not None,
    }
    log.write({"phase": "resumen", **summary})
    report_paths = _write_multimedia_reports(destination_path, rows, summary, log.path)
    if progress_callback:
        progress_callback(
            {
                "phase": "verificacion multimedia completada",
                "current": 1,
                "total": 1,
                "progress_mode": "percent",
                "message": summary["estado"],
            }
        )
    return MultimediaVerificationResult(
        destination=destination_path,
        rows=rows,
        summary=summary,
        report_paths=report_paths,
    )
