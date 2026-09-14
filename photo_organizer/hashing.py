from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from .config import AppConfig
from .exceptions import OperationCancelled


def compute_hash(
    path: Path,
    config: AppConfig,
    cancel_event: threading.Event | None = None,
) -> str:
    hasher = hashlib.new(config.hash_algorithm)
    block_size = config.normalized_block_size()
    with path.open("rb") as handle:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelled("Operacion cancelada durante el calculo de hash")
            block = handle.read(block_size)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()
