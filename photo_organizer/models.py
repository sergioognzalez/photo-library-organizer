from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class MediaType(str, Enum):
    PHOTO = "foto"
    VIDEO = "video"


class DateSource(str, Enum):
    METADATA = "metadata"
    FILENAME = "filename"
    FILESYSTEM = "filesystem"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class MediaFile:
    id: str
    absolute_path: Path
    relative_path: Path
    name: str
    extension: str
    media_type: MediaType
    size: int
    modified_time: float
    detected_date: datetime | None = None
    date_source: DateSource = DateSource.UNKNOWN
    hash: str | None = None
    duplicate_group: str | None = None
    analysis_status: str = "pending"
    planned_destination: Path | None = None
    copy_status: str = "pending"
    verification_status: str = "pending"
    error: str | None = None
    last_reviewed: datetime | None = None

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_group is not None

    def to_report_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ruta_original": str(self.absolute_path),
            "ruta_relativa": str(self.relative_path),
            "nombre": self.name,
            "extension": self.extension,
            "tipo": self.media_type.value,
            "tamano": self.size,
            "fecha_modificacion": self.modified_time,
            "fecha_detectada": self.detected_date.isoformat() if self.detected_date else None,
            "fuente_fecha": self.date_source.value,
            "hash": self.hash,
            "estado": "duplicado" if self.is_duplicate else "unico",
            "grupo_duplicados": self.duplicate_group,
            "destino_previsto": str(self.planned_destination) if self.planned_destination else None,
            "estado_analisis": self.analysis_status,
            "estado_copia": self.copy_status,
            "estado_verificacion": self.verification_status,
            "error": self.error,
            "ultima_revision": self.last_reviewed.isoformat() if self.last_reviewed else None,
        }


@dataclass(slots=True)
class IgnoredFile:
    path: Path
    reason: str
    relative_path: Path
    name: str
    extension: str
    size: int | None = None
    copyable: bool = True
    planned_destination: Path | None = None
    copy_status: str = "pending"
    verification_status: str = "pending"
    error: str | None = None

    def to_report_dict(self) -> dict[str, Any]:
        return {
            "ruta_original": str(self.path),
            "ruta_relativa": str(self.relative_path),
            "nombre": self.name,
            "extension": self.extension,
            "tamano": self.size,
            "motivo": self.reason,
            "copiable": self.copyable,
            "destino_previsto": str(self.planned_destination) if self.planned_destination else None,
            "estado_copia": self.copy_status,
            "estado_verificacion": self.verification_status,
            "error": self.error,
        }


@dataclass(slots=True)
class ScanResult:
    files: list[MediaFile] = field(default_factory=list)
    ignored: list[IgnoredFile] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)
    total_seen: int = 0


@dataclass(slots=True)
class AnalysisResult:
    source: Path
    destination: Path
    files: list[MediaFile]
    ignored: list[IgnoredFile]
    errors: list[tuple[Path, str]]
    summary: dict[str, Any]
    database_path: Path
    report_paths: dict[str, Path] = field(default_factory=dict)


@dataclass(slots=True)
class CopyResult:
    copied_files: int = 0
    verified_files: int = 0
    failed_files: int = 0
    copied_ignored_files: int = 0
    verified_ignored_files: int = 0
    failed_ignored_files: int = 0
    reused_files: int = 0
    reused_ignored_files: int = 0
    copied_bytes: int = 0
    cancelled: bool = False
    report_paths: dict[str, Path] = field(default_factory=dict)


@dataclass(slots=True)
class ResumeItem:
    kind: str
    source_path: Path
    relative_path: Path
    destination_path: Path
    size: int
    status: str
    expected_hash: str | None = None
    message: str | None = None
    media_file: MediaFile | None = None
    ignored_file: IgnoredFile | None = None


@dataclass(slots=True)
class ResumeInspection:
    source: Path
    destination: Path
    database_path: Path
    files: list[MediaFile]
    ignored: list[IgnoredFile]
    items: list[ResumeItem]
    temp_files: list[Path]
    latest_report_path: Path | None
    technical_log_paths: list[Path]
    summary: dict[str, Any]
    extra_files: list[Path] = field(default_factory=list)


@dataclass(slots=True)
class FinalVerificationRow:
    source_path: Path
    destination_path: Path | None
    source_size: int | None
    destination_size: int | None
    source_hash: str | None
    destination_hash: str | None
    result: str
    error_reason: str | None = None

    def to_csv_dict(self) -> dict[str, Any]:
        return {
            "ruta_origen": str(self.source_path),
            "ruta_destino": str(self.destination_path) if self.destination_path else "",
            "tamaño_origen": self.source_size,
            "tamaño_destino": self.destination_size,
            "hash_origen": self.source_hash or "",
            "hash_destino": self.destination_hash or "",
            "resultado": self.result,
            "motivo_error": self.error_reason or "",
        }


@dataclass(slots=True)
class FinalVerificationResult:
    source: Path
    destination: Path
    rows: list[FinalVerificationRow]
    summary: dict[str, Any]
    report_paths: dict[str, Path] = field(default_factory=dict)
    temp_files: list[Path] = field(default_factory=list)
    unplanned_destination_files: list[Path] = field(default_factory=list)
    unexpected_duplicate_files: list[Path] = field(default_factory=list)
    duplicate_planned_destinations: list[Path] = field(default_factory=list)
    planned_missing_from_source: list[Path] = field(default_factory=list)
    cancelled: bool = False

    @property
    def passed(self) -> bool:
        return bool(self.summary.get("verificacion_final_superada", False))


@dataclass(slots=True)
class MultimediaVerificationRow:
    path: Path
    media_type: str
    result: str
    error_reason: str | None = None

    def to_csv_dict(self) -> dict[str, Any]:
        return {
            "ruta": str(self.path),
            "tipo": self.media_type,
            "resultado": self.result,
            "motivo_error": self.error_reason or "",
        }


@dataclass(slots=True)
class MultimediaVerificationResult:
    destination: Path
    rows: list[MultimediaVerificationRow]
    summary: dict[str, Any]
    report_paths: dict[str, Path] = field(default_factory=dict)
    cancelled: bool = False


ProgressCallback = Any
