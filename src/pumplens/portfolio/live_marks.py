"""Fresh public mark prices for portfolio risk. / Свежие публичные mark price для риска."""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

from pumplens.domain.models import MarkPrice


@dataclass(frozen=True, slots=True)
class LiveMark:
    """One received public mark price. / Одна полученная публичная mark price."""

    price: Decimal
    event_time_ms: int
    received_monotonic: float


class LiveMarkPriceStore:
    """Small in-process cache shared by scanner and risk monitor. / Общий кеш процесса."""

    def __init__(self) -> None:
        self._marks: dict[str, LiveMark] = {}

    def update(self, mark: MarkPrice) -> None:
        if mark.mark_price <= 0:
            return
        self._marks[mark.symbol] = LiveMark(
            price=Decimal(str(mark.mark_price)),
            event_time_ms=mark.event_time_ms,
            received_monotonic=time.monotonic(),
        )

    def get(
        self,
        symbol: str,
        *,
        max_age_seconds: float,
        now_monotonic: float | None = None,
    ) -> LiveMark | None:
        mark = self._marks.get(symbol)
        if mark is None:
            return None
        now = time.monotonic() if now_monotonic is None else now_monotonic
        if now - mark.received_monotonic > max_age_seconds:
            return None
        return mark
