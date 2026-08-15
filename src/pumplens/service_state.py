"""Read-only live view shared by CLI and Telegram. / Общий read-only live-срез."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pumplens.domain.models import Candidate


@dataclass(frozen=True, slots=True)
class ScannerStatus:
    updated_at: datetime | None
    universe_size: int
    candidates_count: int
    stale: bool


class ServiceState:
    def __init__(self) -> None:
        self._candidates: tuple[Candidate, ...] = ()
        self._updated_at: datetime | None = None
        self._universe_size = 0

    def update(self, candidates: list[Candidate], universe_size: int) -> None:
        self._candidates = tuple(candidates)
        self._updated_at = datetime.now(UTC)
        self._universe_size = universe_size

    def top(self, limit: int = 10) -> tuple[Candidate, ...]:
        return self._candidates[:limit]

    def status(self) -> ScannerStatus:
        age = (
            (datetime.now(UTC) - self._updated_at).total_seconds()
            if self._updated_at is not None
            else float("inf")
        )
        return ScannerStatus(
            updated_at=self._updated_at,
            universe_size=self._universe_size,
            candidates_count=sum(item.selected for item in self._candidates),
            stale=age > 15,
        )
