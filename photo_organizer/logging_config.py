from __future__ import annotations

import logging
from pathlib import Path

from .config import REPORTS_DIR


def configure_file_logging(destination: Path) -> Path:
    reports_dir = destination / REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    log_path = reports_dir / "app_foto.log"
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        encoding="utf-8",
    )
    return log_path
