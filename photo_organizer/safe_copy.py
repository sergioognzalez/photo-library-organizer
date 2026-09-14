from __future__ import annotations

import hashlib
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig
from .exceptions import CopyVerificationError, OperationCancelled


@dataclass(slots=True)
class CopyOutcome:
    destination: Path
    bytes_copied: int
    source_hash: str
    destination_hash: str


def unique_destination(path: Path) -> Path:
    candidate = path
    counter = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}__{counter}{path.suffix}")
        counter += 1
    return candidate


def cleanup_temp_files(destination_root: Path) -> None:
    for path in destination_root.rglob("*.codexcopy-*.tmp"):
        try:
            path.unlink()
        except OSError:
            pass


def _hash_file(path: Path, config: AppConfig, cancel_event: threading.Event | None = None) -> str:
    hasher = hashlib.new(config.hash_algorithm)
    with path.open("rb") as handle:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelled("Operacion cancelada durante el calculo de hash")
            block = handle.read(config.normalized_block_size())
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


def hash_file(path: Path, config: AppConfig, cancel_event: threading.Event | None = None) -> str:
    return _hash_file(path, config, cancel_event=cancel_event)


def _copy_to_final_destination(
    source: Path,
    final_destination: Path,
    config: AppConfig,
    expected_hash: str | None = None,
    cancel_event: threading.Event | None = None,
    progress_callback=None,
) -> CopyOutcome:
    final_destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = final_destination.with_name(
        f"{final_destination.name}.codexcopy-{uuid.uuid4().hex}.tmp"
    )
    block_size = config.normalized_block_size()
    source_hasher = hashlib.new(config.hash_algorithm)
    copied = 0

    try:
        with source.open("rb") as src, temp_path.open("xb") as dst:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise OperationCancelled("Copia cancelada")
                block = src.read(block_size)
                if not block:
                    break
                dst.write(block)
                source_hasher.update(block)
                copied += len(block)
                if progress_callback:
                    progress_callback({"current_file": str(source), "bytes_copied_delta": len(block)})

        source_size = source.stat().st_size
        temp_size = temp_path.stat().st_size
        if source_size != temp_size:
            raise CopyVerificationError(
                f"Tamano distinto tras copiar: origen={source_size}, temporal={temp_size}"
            )

        source_hash = source_hasher.hexdigest()
        destination_hash = _hash_file(temp_path, config, cancel_event=cancel_event)

        if expected_hash is not None and expected_hash != source_hash:
            raise CopyVerificationError("El hash esperado no coincide con el origen")
        if source_hash != destination_hash:
            raise CopyVerificationError("El hash del temporal no coincide con el origen")

        if final_destination.exists():
            raise CopyVerificationError(f"El destino ya existe y no se sobrescribe: {final_destination}")
        temp_path.rename(final_destination)
        return CopyOutcome(final_destination, copied, source_hash, destination_hash)
    except Exception:
        try:
            if temp_path.exists():
                temp_path.unlink()
        finally:
            raise


def copy_file_verified(
    source: Path,
    destination: Path,
    config: AppConfig,
    expected_hash: str | None = None,
    cancel_event: threading.Event | None = None,
    progress_callback=None,
) -> CopyOutcome:
    """Copy a file to a temporary name, verify it, then rename it atomically."""

    final_destination = unique_destination(destination)
    return _copy_to_final_destination(
        source,
        final_destination,
        config,
        expected_hash=expected_hash,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
    )


def copy_file_verified_exact(
    source: Path,
    destination: Path,
    config: AppConfig,
    expected_hash: str | None = None,
    cancel_event: threading.Event | None = None,
    progress_callback=None,
) -> CopyOutcome:
    """Copy to the exact planned destination; used after resume reconciliation."""

    return _copy_to_final_destination(
        source,
        destination,
        config,
        expected_hash=expected_hash,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
    )
