from __future__ import annotations

import threading
from collections import defaultdict

from .config import AppConfig
from .hashing import compute_hash
from .models import MediaFile


def detect_duplicates(
    files: list[MediaFile],
    config: AppConfig,
    cancel_event: threading.Event | None = None,
    progress_callback=None,
) -> list[MediaFile]:
    """Assign duplicate group labels to exact byte-identical files."""

    by_size: dict[int, list[MediaFile]] = defaultdict(list)
    for media_file in files:
        if media_file.analysis_status != "error":
            by_size[media_file.size].append(media_file)

    hash_groups: list[tuple[str, list[MediaFile]]] = []
    candidate_files = sum(len(group) for group in by_size.values() if len(group) > 1)
    hashed = 0

    for size in sorted(by_size):
        same_size = by_size[size]
        if len(same_size) < 2:
            continue

        by_hash: dict[str, list[MediaFile]] = defaultdict(list)
        for media_file in sorted(same_size, key=lambda item: str(item.relative_path).lower()):
            if media_file.hash is None:
                try:
                    media_file.hash = compute_hash(media_file.absolute_path, config, cancel_event)
                except Exception as exc:
                    media_file.analysis_status = "error"
                    media_file.error = str(exc)
                    if progress_callback:
                        progress_callback(
                            {
                                "phase": "calculo de hashes",
                                "current_file": str(media_file.absolute_path),
                                "hashed": hashed,
                                "hash_candidates": candidate_files,
                                "message": f"Error calculando hash: {media_file.absolute_path}: {exc}",
                                "progress_mode": "busy",
                            }
                        )
                    continue
            by_hash[media_file.hash].append(media_file)
            hashed += 1
            if progress_callback:
                progress_callback(
                    {
                        "phase": "calculo de hashes",
                        "current_file": str(media_file.absolute_path),
                        "hashed": hashed,
                        "hash_candidates": candidate_files,
                    }
                )

        for file_hash, group in by_hash.items():
            if len(group) > 1:
                hash_groups.append((file_hash, group))

    hash_groups.sort(key=lambda item: (min(str(file.relative_path).lower() for file in item[1]), item[0]))
    for index, (_, group) in enumerate(hash_groups, start=1):
        group_label = f"Duplicado {index:04d}"
        for media_file in group:
            media_file.duplicate_group = group_label
            media_file.analysis_status = "duplicate"

    for media_file in files:
        if media_file.analysis_status not in {"error", "duplicate"}:
            media_file.analysis_status = "unique"
            media_file.duplicate_group = None

    return files
