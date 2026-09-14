from __future__ import annotations

import csv
import json
import math
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import APP_DATA_DIR, DATABASE_NAME, REPORTS_DIR, AppConfig
from .database import MediaDatabase
from .exceptions import OperationCancelled
from .models import (
    AnalysisResult,
    CopyResult,
    DateSource,
    IgnoredFile,
    MediaFile,
    MediaType,
    ResumeInspection,
    ResumeItem,
)
from .reports import write_reports
from .safe_copy import copy_file_verified_exact, hash_file


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


def _database_path(destination: Path) -> Path:
    return destination / APP_DATA_DIR / DATABASE_NAME


def _latest_json_report(destination: Path) -> Path | None:
    reports_dir = destination / REPORTS_DIR
    if not reports_dir.exists():
        return None
    candidates = sorted(
        reports_dir.glob("informe_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _technical_logs(destination: Path) -> list[Path]:
    reports_dir = destination / REPORTS_DIR
    if not reports_dir.exists():
        return []
    patterns = ("*tecnico*.log", "*operaciones.log", "*errores.log")
    found: dict[str, Path] = {}
    for pattern in patterns:
        for path in reports_dir.glob(pattern):
            found[str(path)] = path
    return sorted(found.values(), key=lambda path: path.stat().st_mtime, reverse=True)


def _source_from_item(path: Path, relative_path: Path) -> Path:
    source = path
    for _ in relative_path.parts:
        source = source.parent
    return source


def _infer_source(files: list[MediaFile], ignored: list[IgnoredFile], source_hint: Path | None) -> Path:
    candidates: list[Path] = []
    for media_file in files:
        candidates.append(_source_from_item(media_file.absolute_path, media_file.relative_path))
    for ignored_file in ignored:
        candidates.append(_source_from_item(ignored_file.path, ignored_file.relative_path))
    if source_hint is not None:
        hinted = source_hint.expanduser().resolve(strict=False)
        if candidates and any(candidate.resolve(strict=False) != hinted for candidate in candidates):
            raise ValueError("La carpeta de origen indicada no coincide con el plan anterior")
        return hinted
    if not candidates:
        raise ValueError("No se pudo inferir la carpeta de origen desde SQLite o informes")
    first = candidates[0].resolve(strict=False)
    if any(candidate.resolve(strict=False) != first for candidate in candidates):
        raise ValueError("El plan anterior contiene mas de una carpeta de origen")
    return first


def _media_from_report(record: dict[str, Any]) -> MediaFile:
    detected_date = None
    if record.get("fecha_detectada"):
        try:
            detected_date = datetime.fromisoformat(str(record["fecha_detectada"]))
        except ValueError:
            detected_date = None
    return MediaFile(
        id=str(record["id"]),
        absolute_path=Path(str(record["ruta_original"])),
        relative_path=Path(str(record["ruta_relativa"])),
        name=str(record["nombre"]),
        extension=str(record["extension"]),
        media_type=MediaType(str(record["tipo"])),
        size=int(record["tamano"]),
        modified_time=float(record["fecha_modificacion"]),
        detected_date=detected_date,
        date_source=DateSource(str(record["fuente_fecha"])),
        hash=record.get("hash"),
        duplicate_group=record.get("grupo_duplicados"),
        analysis_status=str(record.get("estado_analisis") or "pending"),
        planned_destination=Path(str(record["destino_previsto"])) if record.get("destino_previsto") else None,
        copy_status=str(record.get("estado_copia") or "pending"),
        verification_status=str(record.get("estado_verificacion") or "pending"),
        error=record.get("error"),
    )


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "si", "yes", "y"}


def _ignored_from_report(record: dict[str, Any]) -> IgnoredFile:
    return IgnoredFile(
        path=Path(str(record["ruta_original"])),
        relative_path=Path(str(record["ruta_relativa"])),
        name=str(record["nombre"]),
        extension=str(record.get("extension") or ""),
        size=int(record["tamano"]) if record.get("tamano") not in (None, "") else None,
        reason=str(record.get("motivo") or "ignorado"),
        copyable=_as_bool(record.get("copiable"), default=True),
        planned_destination=Path(str(record["destino_previsto"])) if record.get("destino_previsto") else None,
        copy_status=str(record.get("estado_copia") or "pending"),
        verification_status=str(record.get("estado_verificacion") or "pending"),
        error=record.get("error"),
    )


def _load_report_payload(report_path: Path | None) -> dict[str, Any]:
    if report_path is None:
        return {}
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _ignored_from_csv(destination: Path) -> list[IgnoredFile]:
    path = destination / REPORTS_DIR / "archivos_ignorados.csv"
    if not path.exists():
        return []
    ignored: list[IgnoredFile] = []
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                ignored.append(_ignored_from_report(row))
    except OSError:
        return []
    return ignored


def load_previous_analysis(
    destination: Path,
    source_hint: Path | None = None,
) -> tuple[AnalysisResult, Path | None, list[Path]]:
    database_path = _database_path(destination)
    if not database_path.exists():
        raise ValueError(f"No existe el indice SQLite esperado: {database_path}")

    database = MediaDatabase(database_path)
    files = database.load_files()
    ignored = database.load_ignored_files()
    latest_report = _latest_json_report(destination)
    payload = _load_report_payload(latest_report)

    if not files and payload.get("archivos"):
        files = [_media_from_report(record) for record in payload["archivos"]]
    if not ignored and payload.get("archivos_ignorados"):
        ignored = [_ignored_from_report(record) for record in payload["archivos_ignorados"]]
    if not ignored:
        ignored = _ignored_from_csv(destination)

    source = _infer_source(files, ignored, source_hint)
    summary = payload.get("resumen", {}) if isinstance(payload.get("resumen"), dict) else {}
    result = AnalysisResult(
        source=source,
        destination=destination,
        files=files,
        ignored=ignored,
        errors=[],
        summary=summary,
        database_path=database_path,
        report_paths={"json": latest_report} if latest_report else {},
    )
    return result, latest_report, _technical_logs(destination)


def _source_is_unchanged(media_file: MediaFile) -> tuple[bool, str | None]:
    try:
        source_stat = media_file.absolute_path.stat()
    except OSError as exc:
        return False, f"No se puede leer el origen: {exc}"
    if source_stat.st_size != media_file.size:
        return False, "El tamano del origen cambio desde el analisis"
    if source_stat.st_mtime != media_file.modified_time:
        return False, "La fecha de modificacion del origen cambio desde el analisis"
    return True, None


def _ensure_expected_hash(
    item: ResumeItem,
    config: AppConfig,
    cancel_event: threading.Event | None = None,
) -> str:
    if item.expected_hash:
        return item.expected_hash
    source_hash = hash_file(item.source_path, config, cancel_event=cancel_event)
    item.expected_hash = source_hash
    if item.media_file is not None:
        item.media_file.hash = source_hash
    return source_hash


def _inspect_item(
    item: ResumeItem,
    config: AppConfig,
    source_root: Path,
    destination_root: Path,
    cancel_event: threading.Event | None = None,
) -> ResumeItem:
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled("Reanudacion cancelada durante la verificacion")
    if not _is_relative_to(item.source_path, source_root):
        item.status = "error"
        item.message = "El origen planificado queda fuera de la carpeta de origen"
        return item
    if not _is_relative_to(item.destination_path, destination_root):
        item.status = "error"
        item.message = "El destino planificado queda fuera de la carpeta de destino"
        return item
    if item.media_file is not None:
        unchanged, message = _source_is_unchanged(item.media_file)
        if not unchanged:
            item.status = "error"
            item.message = message
            return item
    else:
        try:
            source_stat = item.source_path.stat()
        except OSError as exc:
            item.status = "error"
            item.message = f"No se puede leer el origen: {exc}"
            return item
        if source_stat.st_size != item.size:
            item.status = "error"
            item.message = "El tamano del origen ignorado cambio desde el analisis"
            return item

    if not item.destination_path.exists():
        item.status = "pending"
        item.message = "Destino no existe"
        return item
    if not item.destination_path.is_file():
        item.status = "error"
        item.message = "El destino existe pero no es un archivo"
        return item

    try:
        destination_size = item.destination_path.stat().st_size
    except OSError as exc:
        item.status = "error"
        item.message = f"No se puede leer el destino: {exc}"
        return item

    if destination_size != item.size:
        item.status = "incomplete"
        item.message = f"Tamano incorrecto: destino={destination_size}, esperado={item.size}"
        return item

    try:
        expected_hash = _ensure_expected_hash(item, config, cancel_event=cancel_event)
        destination_hash = hash_file(item.destination_path, config, cancel_event=cancel_event)
    except OSError as exc:
        item.status = "error"
        item.message = f"No se pudo verificar hash: {exc}"
        return item

    if destination_hash == expected_hash:
        item.status = "verified"
        item.message = "Destino verificado y reutilizable"
    else:
        item.status = "corrupt"
        item.message = "Tamano correcto pero hash distinto"
    return item


def _planned_items(analysis: AnalysisResult) -> list[ResumeItem]:
    items: list[ResumeItem] = []
    missing_destinations = 0
    for media_file in analysis.files:
        if media_file.analysis_status == "error" or media_file.planned_destination is None:
            if media_file.analysis_status != "error":
                missing_destinations += 1
            continue
        items.append(
            ResumeItem(
                kind="media",
                source_path=media_file.absolute_path,
                relative_path=media_file.relative_path,
                destination_path=media_file.planned_destination,
                size=media_file.size,
                status="pending",
                expected_hash=media_file.hash,
                media_file=media_file,
            )
        )
    for ignored_file in analysis.ignored:
        if not ignored_file.copyable or ignored_file.planned_destination is None:
            if ignored_file.copyable and ignored_file.copy_status != "not_applicable":
                missing_destinations += 1
            continue
        items.append(
            ResumeItem(
                kind="ignored",
                source_path=ignored_file.path,
                relative_path=ignored_file.relative_path,
                destination_path=ignored_file.planned_destination,
                size=ignored_file.size or 0,
                status="pending",
                ignored_file=ignored_file,
            )
        )
    if missing_destinations:
        raise ValueError(
            "El plan anterior no contiene destino previsto para "
            f"{missing_destinations} archivos; hay que repetir el analisis."
        )
    return items


def _scan_zone_files(destination: Path) -> list[Path]:
    roots = [destination / "COLECCION_ORDENADA", destination / "DUPLICADOS", destination / "OTROS_ARCHIVOS"]
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        try:
            files.extend(path for path in root.rglob("*") if path.is_file())
        except OSError:
            continue
    return sorted(files, key=lambda path: str(path).lower())


def _temp_files(destination: Path) -> list[Path]:
    temp_files: list[Path] = []
    for path in _scan_zone_files(destination):
        name = path.name.lower()
        if ".codexcopy-" in name and name.endswith(".tmp"):
            temp_files.append(path)
    return sorted(temp_files, key=lambda path: str(path).lower())


def _extra_files(destination: Path, items: list[ResumeItem], temp_files: list[Path]) -> list[Path]:
    planned = {item.destination_path.resolve(strict=False) for item in items}
    temporary = {path.resolve(strict=False) for path in temp_files}
    extra: list[Path] = []
    for path in _scan_zone_files(destination):
        resolved = path.resolve(strict=False)
        if resolved in planned or resolved in temporary:
            continue
        if path.name.lower() == "origenes.json":
            continue
        extra.append(path)
    return sorted(extra, key=lambda path: str(path).lower())


def _inspection_summary(
    items: list[ResumeItem],
    temp_files: list[Path],
    extra_files: list[Path],
    destination: Path,
    config: AppConfig,
) -> dict[str, Any]:
    counts = {
        "verificados_reutilizables": sum(1 for item in items if item.status == "verified"),
        "pendientes": sum(1 for item in items if item.status == "pending"),
        "incompletos": sum(1 for item in items if item.status == "incomplete"),
        "corruptos": sum(1 for item in items if item.status == "corrupt"),
        "errores": sum(1 for item in items if item.status == "error"),
        "temporales_detectados": len(temp_files),
        "archivos_no_planificados_detectados": len(extra_files),
    }
    remaining_items = [item for item in items if item.status in {"pending", "incomplete", "corrupt"}]
    invalid_items = [item for item in items if item.status in {"incomplete", "corrupt"}]
    remaining_bytes = sum(item.size for item in remaining_items)
    invalid_destination_bytes = 0
    for item in invalid_items:
        try:
            invalid_destination_bytes += item.destination_path.stat().st_size
        except OSError:
            pass
    temp_bytes = 0
    for path in temp_files:
        try:
            temp_bytes += path.stat().st_size
        except OSError:
            pass
    margin_bytes = math.ceil(remaining_bytes * config.normalized_safety_margin_percent() / 100)
    needed = remaining_bytes + margin_bytes
    extra_bytes = 0
    for path in extra_files:
        try:
            extra_bytes += path.stat().st_size
        except OSError:
            pass
    free = shutil.disk_usage(destination).free
    available_after_cleanup = free + invalid_destination_bytes + temp_bytes
    summary: dict[str, Any] = {
        **counts,
        "espacio_suficiente_para_reanudar": available_after_cleanup >= needed,
    }
    _add_size(summary, "bytes_restantes_por_copiar", remaining_bytes)
    _add_size(summary, "margen_seguridad_reanudacion", margin_bytes)
    _add_size(summary, "espacio_necesario_restante", needed)
    _add_size(summary, "espacio_libre_actual", free)
    _add_size(summary, "espacio_recuperable_de_destinos_invalidos", invalid_destination_bytes)
    _add_size(summary, "espacio_recuperable_de_temporales", temp_bytes)
    _add_size(summary, "tamano_archivos_no_planificados", extra_bytes)
    _add_size(summary, "espacio_disponible_estimado_tras_limpieza", available_after_cleanup)
    _add_size(summary, "espacio_faltante_para_reanudar", max(0, needed - available_after_cleanup))
    return summary


def _save_item_state(database: MediaDatabase, item: ResumeItem) -> None:
    if item.media_file is not None:
        if item.status == "verified":
            item.media_file.copy_status = "copied"
            item.media_file.verification_status = "verified"
            item.media_file.error = None
        elif item.status == "error":
            item.media_file.copy_status = "failed"
            item.media_file.verification_status = "failed"
            item.media_file.error = item.message
        else:
            item.media_file.copy_status = "pending"
            item.media_file.verification_status = item.status
            item.media_file.error = item.message
        database.save_file(item.media_file)
    elif item.ignored_file is not None:
        if item.status == "verified":
            item.ignored_file.copy_status = "copied"
            item.ignored_file.verification_status = "verified"
            item.ignored_file.error = None
        elif item.status == "error":
            item.ignored_file.copy_status = "failed"
            item.ignored_file.verification_status = "failed"
            item.ignored_file.error = item.message
        else:
            item.ignored_file.copy_status = "pending"
            item.ignored_file.verification_status = item.status
            item.ignored_file.error = item.message
        database.save_ignored_file(item.ignored_file)


def inspect_interrupted_organization(
    analysis: AnalysisResult,
    latest_report_path: Path | None,
    technical_log_paths: list[Path],
    config: AppConfig,
    cancel_event: threading.Event | None = None,
    progress_callback=None,
) -> ResumeInspection:
    items = _planned_items(analysis)
    database = MediaDatabase(analysis.database_path)
    total = len(items)
    for index, item in enumerate(items, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled("Reanudacion cancelada durante la verificacion")
        if progress_callback:
            progress_callback(
                {
                    "phase": "verificando reanudacion",
                    "current_file": str(item.destination_path),
                    "current": index,
                    "total": total,
                    "message": f"Verificando destino planificado: {item.destination_path}",
                    "ui_message": False,
                    "progress_mode": "percent",
                }
            )
        _inspect_item(
            item,
            config,
            analysis.source,
            analysis.destination,
            cancel_event=cancel_event,
        )
        _save_item_state(database, item)

    temp_files = _temp_files(analysis.destination)
    extra_files = _extra_files(analysis.destination, items, temp_files)
    summary = _inspection_summary(items, temp_files, extra_files, analysis.destination, config)
    return ResumeInspection(
        source=analysis.source,
        destination=analysis.destination,
        database_path=analysis.database_path,
        files=analysis.files,
        ignored=analysis.ignored,
        items=items,
        temp_files=temp_files,
        extra_files=extra_files,
        latest_report_path=latest_report_path,
        technical_log_paths=technical_log_paths,
        summary=summary,
    )


def _safe_unlink_destination(path: Path, destination: Path) -> None:
    if not path.exists():
        return
    if not _is_relative_to(path, destination):
        raise ValueError(f"No se elimina una ruta fuera del destino: {path}")
    if not path.is_file():
        raise ValueError(f"No se elimina porque no es un archivo: {path}")
    path.unlink()


def resume_copy(
    inspection: ResumeInspection,
    config: AppConfig,
    cancel_event: threading.Event | None = None,
    progress_callback=None,
) -> CopyResult:
    database = MediaDatabase(inspection.database_path)
    result = CopyResult()
    start = datetime.now()

    for temp_file in inspection.temp_files:
        if cancel_event is not None and cancel_event.is_set():
            result.cancelled = True
            break
        _safe_unlink_destination(temp_file, inspection.destination)

    actionable = [item for item in inspection.items if item.status != "error"]
    total = len(actionable)
    for index, item in enumerate(actionable, start=1):
        if result.cancelled:
            break
        if cancel_event is not None and cancel_event.is_set():
            result.cancelled = True
            break
        if progress_callback:
            progress_callback(
                {
                    "phase": "reanudando organizacion",
                    "current_file": str(item.destination_path),
                    "current": index,
                    "total": total,
                    "copied_files": result.copied_files,
                    "verified_files": result.verified_files,
                    "failed_files": result.failed_files,
                    "bytes_copied": result.copied_bytes,
                    "elapsed_seconds": round((datetime.now() - start).total_seconds(), 1),
                }
            )

        try:
            refreshed = _inspect_item(
                item,
                config,
                inspection.source,
                inspection.destination,
                cancel_event=cancel_event,
            )
        except OperationCancelled:
            result.cancelled = True
            break
        if refreshed.status == "verified":
            if item.media_file is not None:
                item.media_file.copy_status = "copied"
                item.media_file.verification_status = "verified"
                database.save_file(item.media_file)
                result.reused_files += 1
            elif item.ignored_file is not None:
                item.ignored_file.copy_status = "copied"
                item.ignored_file.verification_status = "verified"
                database.save_ignored_file(item.ignored_file)
                result.reused_ignored_files += 1
            continue

        if refreshed.status in {"incomplete", "corrupt"}:
            _safe_unlink_destination(item.destination_path, inspection.destination)
        elif refreshed.status == "error":
            if item.media_file is not None:
                item.media_file.copy_status = "failed"
                item.media_file.verification_status = "failed"
                item.media_file.error = item.message
                database.save_file(item.media_file)
                result.failed_files += 1
            elif item.ignored_file is not None:
                item.ignored_file.copy_status = "failed"
                item.ignored_file.verification_status = "failed"
                item.ignored_file.error = item.message
                database.save_ignored_file(item.ignored_file)
                result.failed_ignored_files += 1
            continue

        try:
            outcome = copy_file_verified_exact(
                item.source_path,
                item.destination_path,
                config,
                expected_hash=item.expected_hash,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
            )
            if item.media_file is not None:
                item.media_file.planned_destination = outcome.destination
                item.media_file.hash = outcome.source_hash
                item.media_file.copy_status = "copied"
                item.media_file.verification_status = "verified"
                database.save_file(item.media_file)
                result.copied_files += 1
                result.verified_files += 1
            elif item.ignored_file is not None:
                item.ignored_file.planned_destination = outcome.destination
                item.ignored_file.copy_status = "copied"
                item.ignored_file.verification_status = "verified"
                database.save_ignored_file(item.ignored_file)
                result.copied_ignored_files += 1
                result.verified_ignored_files += 1
            result.copied_bytes += outcome.bytes_copied
        except OperationCancelled:
            result.cancelled = True
            break
        except Exception as exc:
            if item.media_file is not None:
                item.media_file.copy_status = "failed"
                item.media_file.verification_status = "failed"
                item.media_file.error = str(exc)
                database.save_file(item.media_file)
                result.failed_files += 1
            elif item.ignored_file is not None:
                item.ignored_file.copy_status = "failed"
                item.ignored_file.verification_status = "failed"
                item.ignored_file.error = str(exc)
                database.save_ignored_file(item.ignored_file)
                result.failed_ignored_files += 1

    final_summary = dict(inspection.summary)
    final_summary.update(
        {
            "reanudar_archivos_copiados": result.copied_files,
            "reanudar_archivos_reutilizados": result.reused_files,
            "reanudar_archivos_fallidos": result.failed_files,
            "reanudar_ignorados_copiados": result.copied_ignored_files,
            "reanudar_ignorados_reutilizados": result.reused_ignored_files,
            "reanudar_ignorados_fallidos": result.failed_ignored_files,
            "reanudar_bytes_copiados": result.copied_bytes,
        }
    )
    result.report_paths = write_reports(
        inspection.destination,
        inspection.files,
        inspection.ignored,
        final_summary,
        [],
        "informe_reanudacion",
    )
    return result
