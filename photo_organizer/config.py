from __future__ import annotations

from dataclasses import dataclass

COLLECTION_DIR = "COLECCION_ORDENADA"
DUPLICATES_DIR = "DUPLICADOS"
OTHER_FILES_DIR = "OTROS_ARCHIVOS"
REPORTS_DIR = "INFORMES"
APP_DATA_DIR = "DATOS_APLICACION"
DATABASE_NAME = "indice.sqlite3"

PHOTO_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".avif",
    ".dng",
    ".raw",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".orf",
    ".rw2",
    ".raf",
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
    ".webm",
    ".wmv",
    ".mts",
    ".m2ts",
    ".3gp",
    ".mpeg",
    ".mpg",
}

IGNORED_NAMES = {
    "thumbs.db",
    ".ds_store",
    "desktop.ini",
    "origenes.json",
}

IGNORED_EXTENSIONS = {
    ".tmp",
    ".temp",
    ".part",
    ".crdownload",
    ".download",
    ".lnk",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".log",
    ".csv",
    ".json",
}

NON_COPYABLE_IGNORED_NAMES = {
    "thumbs.db",
    ".ds_store",
    "desktop.ini",
    "origenes.json",
}

NON_COPYABLE_IGNORED_EXTENSIONS = {
    ".tmp",
    ".temp",
    ".part",
    ".crdownload",
    ".download",
    ".lnk",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".log",
}

MONTH_NAMES = {
    1: "01 - Enero",
    2: "02 - Febrero",
    3: "03 - Marzo",
    4: "04 - Abril",
    5: "05 - Mayo",
    6: "06 - Junio",
    7: "07 - Julio",
    8: "08 - Agosto",
    9: "09 - Septiembre",
    10: "10 - Octubre",
    11: "11 - Noviembre",
    12: "12 - Diciembre",
}


@dataclass(slots=True)
class AppConfig:
    """Central runtime configuration."""

    block_size: int = 4 * 1024 * 1024
    max_parallel_operations: int = 1
    hash_algorithm: str = "sha256"
    exiftool_path: str | None = None
    metadata_enabled: bool = True
    use_filesystem_dates: bool = True
    min_valid_year: int = 1900
    max_valid_year: int = 2100
    safety_margin_percent: float = 5.0
    copy_ignored_files: bool = True

    def normalized_block_size(self) -> int:
        return max(64 * 1024, int(self.block_size))

    def normalized_parallelism(self) -> int:
        return max(1, min(4, int(self.max_parallel_operations)))

    def normalized_safety_margin_percent(self) -> float:
        return max(0.0, min(100.0, float(self.safety_margin_percent)))
