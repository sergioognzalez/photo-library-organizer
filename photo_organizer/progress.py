from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import REPORTS_DIR


class ProgressReporter:
    """Forward progress events to the UI and to a persistent technical log."""

    def __init__(self, callback: Callable[[dict[str, Any]], None] | None = None):
        self.callback = callback
        self.log_path: Path | None = None

    def open_log(self, destination: Path, prefix: str = "analisis_tecnico") -> Path:
        reports_dir = destination / REPORTS_DIR
        reports_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = reports_dir / f"{prefix}_{timestamp}.log"
        self.log_path.write_text("Registro tecnico de analisis\n", encoding="utf-8")
        self.emit(
            {
                "phase": "registro tecnico",
                "message": f"Registro tecnico: {self.log_path}",
                "log_path": str(self.log_path),
                "progress_mode": "busy",
            }
        )
        return self.log_path

    def emit(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload["timestamp"] = datetime.now().isoformat(timespec="seconds")
        self._write(payload)
        if self.callback:
            self.callback(payload)

    def _write(self, payload: dict[str, Any]) -> None:
        if self.log_path is None:
            return
        try:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str))
                handle.write("\n")
        except OSError:
            pass
