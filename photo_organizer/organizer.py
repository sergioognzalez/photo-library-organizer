from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from .config import (
    APP_DATA_DIR,
    COLLECTION_DIR,
    DATABASE_NAME,
    DUPLICATES_DIR,
    OTHER_FILES_DIR,
    REPORTS_DIR,
    AppConfig,
)
from .database import MediaDatabase
from .date_resolver import resolve_media_date
from .duplicate_detector import detect_duplicates
from .exceptions import OperationCancelled, ReuseNotSafeError, ValidationError
from .final_verification import run_final_verification, run_multimedia_verification
from .metadata import ExifToolMetadataReader
from .models import (
    AnalysisResult,
    CopyResult,
    DateSource,
    FinalVerificationResult,
    IgnoredFile,
    MediaFile,
    MediaType,
    MultimediaVerificationResult,
    ResumeInspection,
)
from .planner import assign_destinations, assign_ignored_destinations
from .progress import ProgressReporter
from .reports import update_analysis_report, write_reports
from .resume import (
    inspect_interrupted_organization as inspect_resume_plan,
    load_previous_analysis,
    resume_copy,
)
from .safe_copy import cleanup_temp_files, copy_file_verified_exact
from .scanner import scan_source


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        attrs = getattr(path.stat(), "st_file_attributes", 0)
        return path.is_symlink() or bool(attrs & 0x400)
    except OSError:
        return True


def validate_paths(source: Path, destination: Path, create_destination: bool = True) -> tuple[Path, Path]:
    source = source.expanduser()
    destination = destination.expanduser()

    if not source.exists():
        raise ValidationError("La carpeta de origen no existe")
    source_resolved = source.resolve(strict=True)
    if not source_resolved.is_dir():
        raise ValidationError("El origen no es una carpeta")
    if _is_reparse_or_symlink(source_resolved):
        raise ValidationError("El origen no puede ser un enlace simbolico o punto de union")
    if not os.access(source_resolved, os.R_OK):
        raise ValidationError("La carpeta de origen no se puede leer")
    try:
        with os.scandir(source_resolved):
            pass
    except OSError as exc:
        raise ValidationError(f"La carpeta de origen no se puede leer: {exc}") from exc

    destination_resolved = destination.resolve(strict=False)
    if source_resolved == destination_resolved:
        raise ValidationError("Origen y destino no pueden ser la misma carpeta")
    if _is_relative_to(destination_resolved, source_resolved):
        raise ValidationError("El destino no puede estar dentro del origen")
    if _is_relative_to(source_resolved, destination_resolved):
        raise ValidationError("El origen no puede estar dentro del destino")

    if destination_resolved.exists() and _is_reparse_or_symlink(destination_resolved):
        raise ValidationError("El destino no puede ser un enlace simbolico o punto de union")

    if create_destination:
        try:
            destination_resolved.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValidationError(f"No se pudo crear la carpeta de destino: {exc}") from exc
    elif not destination_resolved.exists():
        raise ValidationError("La carpeta de destino no existe")

    if not destination_resolved.is_dir():
        raise ValidationError("El destino no es una carpeta")

    test_file = destination_resolved / ".app_foto_write_test.tmp"
    try:
        with test_file.open("xb") as handle:
            handle.write(b"test")
        test_file.unlink()
    except OSError as exc:
        try:
            if test_file.exists():
                test_file.unlink()
        except OSError:
            pass
        raise ValidationError(f"La carpeta de destino no permite escribir: {exc}") from exc

    return source_resolved, destination_resolved


def ensure_destination_structure(destination: Path) -> dict[str, Path]:
    paths = {
        "coleccion": destination / COLLECTION_DIR,
        "duplicados": destination / DUPLICATES_DIR,
        "otros": destination / OTHER_FILES_DIR,
        "informes": destination / REPORTS_DIR,
        "datos": destination / APP_DATA_DIR,
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def _database_path(destination: Path) -> Path:
    return destination / APP_DATA_DIR / DATABASE_NAME


def _space_free(destination: Path) -> int:
    return shutil.disk_usage(destination).free


def _format_gb(size: int) -> str:
    return f"{size / (1024 ** 3):.2f} GB"


def _add_size(summary: dict[str, Any], key: str, value: int) -> None:
    summary[key] = value
    summary[f"{key}_gb"] = _format_gb(value)


def _ignored_by_extension(ignored: list[IgnoredFile]) -> dict[str, int]:
    grouped: dict[str, int] = {}
    for ignored_file in ignored:
        extension = ignored_file.extension.upper() if ignored_file.extension else "[SIN_EXTENSION]"
        grouped[extension] = grouped.get(extension, 0) + 1
    return dict(sorted(grouped.items(), key=lambda item: item[0]))


def _summary(
    files: list[MediaFile],
    ignored: list[IgnoredFile],
    errors_count: int,
    destination: Path,
    config: AppConfig,
) -> dict[str, Any]:
    compatible = [file for file in files if file.analysis_status != "error"]
    unique_files = [file for file in compatible if not file.is_duplicate]
    duplicate_files = [file for file in compatible if file.is_duplicate]
    groups = sorted({file.duplicate_group for file in duplicate_files if file.duplicate_group})
    sin_fecha = [
        file
        for file in compatible
        if file.detected_date is None or file.date_source == DateSource.UNKNOWN
    ]
    unique_size = sum(file.size for file in unique_files)
    duplicate_size = sum(file.size for file in duplicate_files)
    duplicate_representative_size = 0
    for group in groups:
        group_files = [file for file in duplicate_files if file.duplicate_group == group]
        if group_files:
            duplicate_representative_size += group_files[0].size
    redundant_copies = max(0, len(duplicate_files) - len(groups))
    redundant_size = max(0, duplicate_size - duplicate_representative_size)
    copyable_ignored = [file for file in ignored if file.copyable]
    noncopyable_ignored = [file for file in ignored if not file.copyable]
    copyable_ignored_size = sum(file.size or 0 for file in copyable_ignored)
    noncopyable_ignored_size = sum(file.size or 0 for file in noncopyable_ignored)
    other_files_size = copyable_ignored_size if config.copy_ignored_files else 0
    collection_size = unique_size
    duplicates_folder_size = duplicate_size
    needed_without_margin = collection_size + duplicates_folder_size + other_files_size
    margin_percent = config.normalized_safety_margin_percent()
    margin_bytes = math.ceil(needed_without_margin * margin_percent / 100)
    needed = needed_without_margin + margin_bytes
    free = _space_free(destination)
    missing = max(0, needed - free)

    summary: dict[str, Any] = {
        "total_archivos_encontrados": len(files) + len(ignored) + errors_count,
        "total_archivos_compatibles": len(compatible),
        "fotos": sum(1 for file in compatible if file.media_type == MediaType.PHOTO),
        "videos": sum(1 for file in compatible if file.media_type == MediaType.VIDEO),
        "archivos_unicos": len(unique_files),
        "grupos_duplicados": len(groups),
        "archivos_en_grupos_duplicados": len(duplicate_files),
        "copias_redundantes": redundant_copies,
        "archivos_sin_fecha": len(sin_fecha),
        "archivos_ignorados": len(ignored),
        "archivos_ignorados_copiables": len(copyable_ignored),
        "archivos_ignorados_no_copiables": len(noncopyable_ignored),
        "archivos_ignorados_por_extension": _ignored_by_extension(ignored),
        "copiar_archivos_ignorados": config.copy_ignored_files,
        "errores": errors_count + sum(1 for file in files if file.analysis_status == "error"),
        "margen_seguridad_porcentaje": margin_percent,
        "espacio_suficiente": free >= needed,
        "aviso_seguridad": (
            "Los archivos originales no seran modificados. "
            "Todo el resultado se creara mediante copias en la carpeta de destino."
        ),
    }
    _add_size(summary, "tamano_total_unicos", unique_size)
    _add_size(summary, "tamano_total_archivos_en_grupos_duplicados", duplicate_size)
    _add_size(summary, "tamano_total_duplicados", duplicate_size)
    _add_size(summary, "tamano_copia_representativa_grupos_duplicados", duplicate_representative_size)
    _add_size(summary, "espacio_redundante_recuperable", redundant_size)
    _add_size(summary, "tamano_final_coleccion_ordenada", collection_size)
    _add_size(summary, "tamano_final_duplicados", duplicates_folder_size)
    _add_size(summary, "tamano_archivos_ignorados_copiables", copyable_ignored_size)
    _add_size(summary, "tamano_archivos_ignorados_no_copiables", noncopyable_ignored_size)
    _add_size(summary, "tamano_final_otros_archivos", other_files_size)
    _add_size(summary, "espacio_total_sin_margen_destino", needed_without_margin)
    _add_size(summary, "margen_seguridad_bytes", margin_bytes)
    _add_size(summary, "espacio_total_estimado_destino", needed)
    _add_size(summary, "espacio_total_necesario_destino", needed)
    _add_size(summary, "espacio_libre_destino", free)
    _add_size(summary, "espacio_faltante_destino", missing)
    return summary


def _media_signature(files: list[MediaFile]) -> dict[str, tuple[int, float, str, str]]:
    return {
        str(file.relative_path).lower(): (
            file.size,
            file.modified_time,
            file.extension.lower(),
            file.media_type.value,
        )
        for file in files
        if file.analysis_status != "error"
    }


class OrganizerService:
    def __init__(self, config: AppConfig | None = None):
        self.config = config or AppConfig()

    def analyze(
        self,
        source: Path | str,
        destination: Path | str,
        cancel_event: threading.Event | None = None,
        progress_callback=None,
        metadata_reader: ExifToolMetadataReader | None = None,
    ) -> AnalysisResult:
        progress = ProgressReporter(progress_callback)
        progress.emit(
            {
                "phase": "validando carpetas",
                "current_file": str(source),
                "message": f"Validando origen {source} y destino {destination}",
                "progress_mode": "busy",
            }
        )
        source_path, destination_path = validate_paths(Path(source), Path(destination))
        progress.emit(
            {
                "phase": "preparando destino",
                "current_file": str(destination_path),
                "message": "Creando estructura de destino y registro tecnico",
                "progress_mode": "busy",
            }
        )
        ensure_destination_structure(destination_path)
        progress.open_log(destination_path)
        database = MediaDatabase(_database_path(destination_path))

        progress.emit(
            {
                "phase": "enumerando archivos",
                "current_file": str(source_path),
                "message": "Escaneando origen de solo lectura",
                "progress_mode": "busy",
            }
        )
        scan_result = scan_source(source_path, cancel_event, progress.emit)
        database.apply_cache(scan_result.files)

        reader = metadata_reader or ExifToolMetadataReader(self.config)
        total = len(scan_result.files)
        for index, media_file in enumerate(scan_result.files, start=1):
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelled("Analisis cancelado durante la lectura de metadatos")
            try:
                current_stat = media_file.absolute_path.stat()
                if current_stat.st_size != media_file.size or current_stat.st_mtime != media_file.modified_time:
                    media_file.analysis_status = "error"
                    media_file.error = "El archivo cambio durante el analisis"
                    progress.emit(
                        {
                            "phase": "lectura de metadatos",
                            "current_file": str(media_file.absolute_path),
                            "current": index,
                            "total": total,
                            "errors": len(scan_result.errors)
                            + sum(1 for file in scan_result.files if file.analysis_status == "error"),
                            "message": f"Archivo cambiado durante el analisis: {media_file.absolute_path}",
                            "progress_mode": "percent",
                        }
                    )
                    continue
            except OSError as exc:
                media_file.analysis_status = "error"
                media_file.error = f"No se puede acceder al archivo durante metadatos: {exc}"
                progress.emit(
                    {
                        "phase": "lectura de metadatos",
                        "current_file": str(media_file.absolute_path),
                        "current": index,
                        "total": total,
                        "errors": len(scan_result.errors)
                        + sum(1 for file in scan_result.files if file.analysis_status == "error"),
                        "message": f"Error accediendo al archivo: {media_file.absolute_path}: {exc}",
                        "progress_mode": "percent",
                    }
                )
                continue
            if media_file.detected_date is None or media_file.date_source == DateSource.UNKNOWN:
                metadata = reader.read(media_file.absolute_path)
                resolution = resolve_media_date(media_file, metadata, self.config)
                media_file.detected_date = resolution.value
                media_file.date_source = resolution.source
            progress.emit(
                {
                    "phase": "lectura de metadatos",
                    "current_file": str(media_file.absolute_path),
                    "current": index,
                    "total": total,
                    "reviewed": index,
                    "compatible": total,
                    "progress_mode": "percent",
                }
            )

        progress.emit(
            {
                "phase": "agrupacion por tamano",
                "message": "Agrupando archivos por tamano",
                "progress_mode": "busy",
            }
        )
        detect_duplicates(scan_result.files, self.config, cancel_event, progress.emit)

        progress.emit(
            {
                "phase": "preparacion del resultado",
                "message": "Calculando destinos previstos sin copiar archivos",
                "progress_mode": "busy",
            }
        )
        assign_destinations(scan_result.files, destination_path)
        assign_ignored_destinations(
            scan_result.ignored,
            destination_path,
            self.config.copy_ignored_files,
        )

        summary = _summary(
            scan_result.files,
            ignored=scan_result.ignored,
            errors_count=len(scan_result.errors),
            destination=destination_path,
            config=self.config,
        )
        progress.emit(
            {
                "phase": "guardando indice",
                "message": "Guardando indice SQLite e informes en destino",
                "progress_mode": "busy",
            }
        )
        database.save_files(scan_result.files)
        database.save_ignored_files(scan_result.ignored)
        result = AnalysisResult(
            source=source_path,
            destination=destination_path,
            files=scan_result.files,
            ignored=scan_result.ignored,
            errors=scan_result.errors,
            summary=summary,
            database_path=database.path,
        )
        result = update_analysis_report(result)
        progress.emit(
            {
                "phase": "analisis completado",
                "message": "Analisis completado. No se ha copiado ningun archivo.",
                "current": 1,
                "total": 1,
                "progress_mode": "percent",
            }
        )
        return result

    def recalculate_from_previous_analysis(
        self,
        analysis: AnalysisResult,
        destination: Path | str,
        cancel_event: threading.Event | None = None,
        progress_callback=None,
    ) -> AnalysisResult:
        progress = ProgressReporter(progress_callback)
        progress.emit(
            {
                "phase": "validando reutilizacion",
                "current_file": str(analysis.source),
                "message": "Validando si el analisis previo puede reutilizarse",
                "progress_mode": "busy",
            }
        )
        if analysis.errors or any(file.analysis_status == "error" for file in analysis.files):
            raise ReuseNotSafeError("El analisis previo contiene errores; se requiere analisis completo")

        source_path, destination_path = validate_paths(analysis.source, Path(destination))
        ensure_destination_structure(destination_path)
        progress.open_log(destination_path, prefix="recalculo_tecnico")
        progress.emit(
            {
                "phase": "validando origen para reutilizar",
                "current_file": str(source_path),
                "message": (
                    "Comprobando ruta, tamano y fecha de modificacion. "
                    "No se recalculan metadatos ni hashes si todo coincide."
                ),
                "progress_mode": "busy",
            }
        )
        validation_scan = scan_source(source_path, cancel_event, progress.emit)
        if validation_scan.errors:
            raise ReuseNotSafeError("El origen produjo errores al validarlo; se requiere analisis completo")

        previous_signature = _media_signature(analysis.files)
        current_signature = _media_signature(validation_scan.files)
        if previous_signature != current_signature:
            raise ReuseNotSafeError(
                "El origen cambio desde el analisis previo; se requiere analisis completo"
            )

        progress.emit(
            {
                "phase": "recalculando destino",
                "message": "Origen validado. Reutilizando fechas, hashes y grupos de duplicados.",
                "progress_mode": "busy",
            }
        )
        analysis.source = source_path
        analysis.destination = destination_path
        analysis.database_path = _database_path(destination_path)
        assign_destinations(analysis.files, destination_path)
        assign_ignored_destinations(
            analysis.ignored,
            destination_path,
            self.config.copy_ignored_files,
        )
        analysis.summary = _summary(
            analysis.files,
            ignored=analysis.ignored,
            errors_count=len(analysis.errors),
            destination=destination_path,
            config=self.config,
        )
        database = MediaDatabase(analysis.database_path)
        database.save_files(analysis.files)
        database.save_ignored_files(analysis.ignored)
        result = update_analysis_report(analysis, prefix="informe_recalculo")
        progress.emit(
            {
                "phase": "recalculo completado",
                "message": "Resumen recalculado sin copiar archivos.",
                "current": 1,
                "total": 1,
                "progress_mode": "percent",
            }
        )
        return result

    def inspect_interrupted_organization(
        self,
        source: Path | str | None,
        destination: Path | str,
        cancel_event: threading.Event | None = None,
        progress_callback=None,
    ) -> ResumeInspection:
        progress = ProgressReporter(progress_callback)
        destination_path = Path(destination).expanduser().resolve(strict=False)
        if not destination_path.exists():
            raise ValidationError("La carpeta de destino no existe; no se puede reanudar")
        if not destination_path.is_dir():
            raise ValidationError("El destino no es una carpeta")

        ensure_destination_structure(destination_path)
        progress.open_log(destination_path, prefix="reanudacion_tecnica")
        progress.emit(
            {
                "phase": "leyendo ejecucion anterior",
                "current_file": str(destination_path),
                "message": "Leyendo indice SQLite, informes y registros tecnicos previos",
                "progress_mode": "busy",
            }
        )
        source_hint = Path(source) if source else None
        analysis, latest_report_path, technical_log_paths = load_previous_analysis(
            destination_path,
            source_hint=source_hint,
        )
        source_path, destination_path = validate_paths(
            analysis.source,
            destination_path,
            create_destination=False,
        )
        analysis.source = source_path
        analysis.destination = destination_path
        analysis.database_path = _database_path(destination_path)

        progress.emit(
            {
                "phase": "verificando reanudacion",
                "current_file": str(destination_path),
                "message": "Comparando el plan anterior con el contenido actual del destino",
                "progress_mode": "busy",
            }
        )
        inspection = inspect_resume_plan(
            analysis,
            latest_report_path,
            technical_log_paths,
            self.config,
            cancel_event=cancel_event,
            progress_callback=progress.emit,
        )
        progress.emit(
            {
                "phase": "reanudacion preparada",
                "message": "Inspeccion de reanudacion completada. No se ha copiado ningun archivo.",
                "current": 1,
                "total": 1,
                "progress_mode": "percent",
            }
        )
        return inspection

    def resume_interrupted_organization(
        self,
        inspection: ResumeInspection,
        cancel_event: threading.Event | None = None,
        progress_callback=None,
    ) -> CopyResult:
        if not inspection.summary.get("espacio_suficiente_para_reanudar", False):
            raise ValidationError("No hay espacio libre suficiente para reanudar")

        progress = ProgressReporter(progress_callback)
        ensure_destination_structure(inspection.destination)
        progress.open_log(inspection.destination, prefix="reanudacion_copia_tecnica")
        progress.emit(
            {
                "phase": "reanudando organizacion",
                "current_file": str(inspection.destination),
                "message": (
                    "Reanudando solo copias pendientes, incompletas o corruptas. "
                    "El origen no se modifica."
                ),
                "progress_mode": "busy",
            }
        )
        result = resume_copy(
            inspection,
            self.config,
            cancel_event=cancel_event,
            progress_callback=progress.emit,
        )
        self._write_duplicate_origins(inspection.files)
        progress.emit(
            {
                "phase": "reanudacion finalizada" if not result.cancelled else "reanudacion cancelada",
                "message": (
                    "Reanudacion finalizada."
                    if not result.cancelled
                    else "Reanudacion cancelada de forma segura."
                ),
                "current": 1,
                "total": 1,
                "progress_mode": "percent",
            }
        )
        return result

    def verify_final_before_delete(
        self,
        source: Path | str,
        destination: Path | str,
        cancel_event: threading.Event | None = None,
        progress_callback=None,
    ) -> FinalVerificationResult:
        return run_final_verification(
            source,
            destination,
            self.config,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )

    def verify_destination_multimedia(
        self,
        source: Path | str | None,
        destination: Path | str,
        cancel_event: threading.Event | None = None,
        progress_callback=None,
    ) -> MultimediaVerificationResult:
        return run_multimedia_verification(
            source,
            destination,
            self.config,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )

    def organize(
        self,
        analysis: AnalysisResult,
        cancel_event: threading.Event | None = None,
        progress_callback=None,
    ) -> CopyResult:
        if not analysis.summary.get("espacio_suficiente", False):
            raise ValidationError("No hay espacio libre suficiente en el destino")

        ensure_destination_structure(analysis.destination)
        cleanup_temp_files(analysis.destination)
        database = MediaDatabase(analysis.database_path)
        result = CopyResult()
        start = time.monotonic()
        files_to_copy = [
            media_file
            for media_file in analysis.files
            if media_file.analysis_status != "error" and media_file.planned_destination is not None
        ]

        for index, media_file in enumerate(files_to_copy, start=1):
            if cancel_event is not None and cancel_event.is_set():
                result.cancelled = True
                break
            try:
                if progress_callback:
                    progress_callback(
                        {
                            "phase": "copia",
                            "current_file": str(media_file.absolute_path),
                            "current": index,
                            "total": len(files_to_copy),
                            "copied_files": result.copied_files,
                            "verified_files": result.verified_files,
                            "failed_files": result.failed_files,
                            "bytes_copied": result.copied_bytes,
                            "elapsed_seconds": round(time.monotonic() - start, 1),
                        }
                    )
                outcome = copy_file_verified_exact(
                    media_file.absolute_path,
                    media_file.planned_destination,
                    self.config,
                    expected_hash=media_file.hash,
                    cancel_event=cancel_event,
                    progress_callback=progress_callback,
                )
                media_file.planned_destination = outcome.destination
                media_file.hash = outcome.source_hash
                media_file.copy_status = "copied"
                media_file.verification_status = "verified"
                result.copied_files += 1
                result.verified_files += 1
                result.copied_bytes += outcome.bytes_copied
                database.save_file(media_file)
            except OperationCancelled:
                result.cancelled = True
                break
            except Exception as exc:
                media_file.copy_status = "failed"
                media_file.verification_status = "failed"
                media_file.error = str(exc)
                result.failed_files += 1
                database.save_file(media_file)

        ignored_to_copy = [
            ignored_file
            for ignored_file in analysis.ignored
            if self.config.copy_ignored_files
            and ignored_file.copyable
            and ignored_file.planned_destination is not None
        ]
        if not result.cancelled:
            for index, ignored_file in enumerate(ignored_to_copy, start=1):
                if cancel_event is not None and cancel_event.is_set():
                    result.cancelled = True
                    break
                try:
                    if progress_callback:
                        progress_callback(
                            {
                                "phase": "copia de otros archivos",
                                "current_file": str(ignored_file.path),
                                "current": index,
                                "total": len(ignored_to_copy),
                                "copied_files": result.copied_files,
                                "verified_files": result.verified_files,
                                "failed_files": result.failed_files,
                                "bytes_copied": result.copied_bytes,
                                "elapsed_seconds": round(time.monotonic() - start, 1),
                            }
                        )
                    outcome = copy_file_verified_exact(
                        ignored_file.path,
                        ignored_file.planned_destination,
                        self.config,
                        expected_hash=None,
                        cancel_event=cancel_event,
                        progress_callback=progress_callback,
                    )
                    ignored_file.planned_destination = outcome.destination
                    ignored_file.copy_status = "copied"
                    ignored_file.verification_status = "verified"
                    result.copied_ignored_files += 1
                    result.verified_ignored_files += 1
                    result.copied_bytes += outcome.bytes_copied
                    database.save_ignored_file(ignored_file)
                except OperationCancelled:
                    result.cancelled = True
                    break
                except Exception as exc:
                    ignored_file.copy_status = "failed"
                    ignored_file.verification_status = "failed"
                    ignored_file.error = str(exc)
                    result.failed_ignored_files += 1
                    database.save_ignored_file(ignored_file)

        self._write_duplicate_origins(analysis.files)
        database.save_files(analysis.files)
        database.save_ignored_files(analysis.ignored)
        final_summary = dict(analysis.summary)
        final_summary.update(
            {
                "archivos_copiados": result.copied_files,
                "archivos_verificados": result.verified_files,
                "archivos_fallidos": result.failed_files,
                "archivos_ignorados_copiados": result.copied_ignored_files,
                "archivos_ignorados_verificados": result.verified_ignored_files,
                "archivos_ignorados_fallidos": result.failed_ignored_files,
                "bytes_copiados": result.copied_bytes,
                "cancelado": result.cancelled,
            }
        )
        result.report_paths = write_reports(
            analysis.destination,
            analysis.files,
            analysis.ignored,
            final_summary,
            analysis.errors,
            "informe_final",
        )
        return result

    def _write_duplicate_origins(self, files: list[MediaFile]) -> None:
        by_group: dict[str, list[MediaFile]] = {}
        for media_file in files:
            if media_file.duplicate_group and media_file.planned_destination:
                by_group.setdefault(media_file.duplicate_group, []).append(media_file)

        for group, group_files in by_group.items():
            group_dir = group_files[0].planned_destination.parent
            group_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "grupo": group,
                "archivos": [media_file.to_report_dict() for media_file in group_files],
            }
            (group_dir / "origenes.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
