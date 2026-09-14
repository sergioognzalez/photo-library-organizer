from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(slots=True)
class CancellationToken:
    event: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self.event.set()

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()


def make_progress_collector() -> tuple[list[dict[str, Any]], Callable[[dict[str, Any]], None]]:
    events: list[dict[str, Any]] = []

    def collect(event: dict[str, Any]) -> None:
        events.append(event)

    return events, collect
