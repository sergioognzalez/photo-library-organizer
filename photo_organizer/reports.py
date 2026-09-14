from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import REPORTS_DIR
from .models import AnalysisResult, IgnoredFile, MediaFile


def _records(files: list[MediaFile]) -> list[dict[str, Any]]:
    return [media_file.to_report_dict() for media_file in files]


def ignored_records(ignored: list[IgnoredFile]) -> list[dict[str, Any]]:
    return [ignored_file.to_report_dict() for ignored_file in ignored]


def ignored_summary_by_extension(ignored: list[IgnoredFile]) -> dict[str, int]:
    grouped: dict[str, int] = {}
    for ignored_file in ignored:
        extension = ignored_file.extension.upper() if ignored_file.extension else "[SIN_EXTENSION]"
        grouped[extension] = grouped.get(extension, 0) + 1
    return dict(sorted(grouped.items(), key=lambda item: item[0]))


def write_ignored_csv(reports_dir: Path, ignored: list[IgnoredFile]) -> Path:
    path = reports_dir / "archivos_ignorados.csv"
    fieldnames = [
        "ruta_original",
        "ruta_relativa",
        "nombre",
        "extension",
        "tamano",
        "motivo",
        "copiable",
        "destino_previsto",
        "estado_copia",
        "estado_verificacion",
        "error",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in ignored_records(ignored):
            writer.writerow(record)
    return path


def write_reports(
    destination: Path,
    files: list[MediaFile],
    ignored: list[IgnoredFile],
    summary: dict[str, Any],
    errors: list[tuple[Path, str]],
    prefix: str,
) -> dict[str, Path]:
    reports_dir = destination / REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = reports_dir / f"{prefix}_{timestamp}.json"
    csv_path = reports_dir / f"{prefix}_{timestamp}.csv"
    operations_log = reports_dir / f"{prefix}_{timestamp}_operaciones.log"
    errors_log = reports_dir / f"{prefix}_{timestamp}_errores.log"
    ignored_csv = write_ignored_csv(reports_dir, ignored)

    payload = {
        "generado_en": datetime.now().isoformat(timespec="seconds"),
        "resumen": summary,
        "archivos": _records(files),
        "archivos_ignorados": ignored_records(ignored),
        "archivos_ignorados_por_extension": ignored_summary_by_extension(ignored),
        "errores": [{"ruta": str(path), "error": message} for path, message in errors],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fieldnames = list(files[0].to_report_dict().keys()) if files else [
        "id",
        "ruta_original",
        "ruta_relativa",
        "nombre",
        "extension",
        "tipo",
        "tamano",
        "fecha_modificacion",
        "fecha_detectada",
        "fuente_fecha",
        "hash",
        "estado",
        "grupo_duplicados",
        "destino_previsto",
        "estado_analisis",
        "estado_copia",
        "estado_verificacion",
        "error",
        "ultima_revision",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in _records(files):
            writer.writerow(record)

    with operations_log.open("w", encoding="utf-8") as handle:
        handle.write("Registro de operaciones\n")
        handle.write(json.dumps(summary, ensure_ascii=False, indent=2))
        handle.write("\n")
        for media_file in files:
            handle.write(
                f"{media_file.analysis_status} | {media_file.copy_status} | "
                f"{media_file.relative_path} -> {media_file.planned_destination}\n"
            )
        handle.write("\nArchivos ignorados por extension\n")
        handle.write(json.dumps(ignored_summary_by_extension(ignored), ensure_ascii=False, indent=2))
        handle.write("\n")

    with errors_log.open("w", encoding="utf-8") as handle:
        for path, message in errors:
            handle.write(f"{path}: {message}\n")
        for media_file in files:
            if media_file.error:
                handle.write(f"{media_file.absolute_path}: {media_file.error}\n")

    return {
        "json": json_path,
        "csv": csv_path,
        "ignorados_csv": ignored_csv,
        "operaciones": operations_log,
        "errores": errors_log,
    }


def update_analysis_report(result: AnalysisResult, prefix: str = "informe_analisis") -> AnalysisResult:
    result.report_paths = write_reports(
        result.destination,
        result.files,
        result.ignored,
        result.summary,
        result.errors,
        prefix,
    )
    return result
