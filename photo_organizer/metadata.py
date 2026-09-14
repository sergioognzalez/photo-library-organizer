from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .config import AppConfig

EXIFTOOL_FIELDS = (
    "DateTimeOriginal",
    "SubSecDateTimeOriginal",
    "CreateDate",
    "MediaCreateDate",
    "TrackCreateDate",
    "CreationDate",
    "DateCreated",
)


class ExifToolMetadataReader:
    """Read metadata with ExifTool when it is installed."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.executable = self._find_exiftool()

    def _find_exiftool(self) -> str | None:
        if not self.config.metadata_enabled:
            return None
        if self.config.exiftool_path:
            candidate = Path(self.config.exiftool_path)
            return str(candidate) if candidate.exists() else None
        return shutil.which("exiftool") or shutil.which("exiftool.exe")

    @property
    def available(self) -> bool:
        return self.executable is not None

    def read(self, path: Path) -> dict[str, object]:
        if not self.executable:
            return {}

        command = [
            self.executable,
            "-j",
            "-api",
            "LargeFileSupport=1",
            *[f"-{field}" for field in EXIFTOOL_FIELDS],
            str(path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}

        if completed.returncode != 0 or not completed.stdout.strip():
            return {}
        try:
            parsed = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return {}
        if not parsed or not isinstance(parsed[0], dict):
            return {}
        return parsed[0]
