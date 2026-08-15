"""Deterministic JSONL event reader. / Детерминированный reader JSONL-событий."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import aiofiles

from pumplens.domain.events import (
    AggTradeEvent,
    BookTickerEvent,
    DepthEvent,
    KlineEvent,
    MarketEvent,
    MarkPriceEvent,
    TickerEvent,
)
from pumplens.domain.models import AggTrade, BookTicker, DepthSnapshot, Kline, MarkPrice, Ticker24h


class ReplayFormatError(ValueError):
    """Malformed or unsupported recording. / Повреждённая или неподдерживаемая запись."""


class ReplayReader:
    def __init__(self, path: Path) -> None:
        self._path = path

    async def events(self) -> AsyncIterator[tuple[int, MarketEvent]]:
        async with aiofiles.open(self._path, encoding="utf-8") as source:
            line_number = 0
            async for line in source:
                line_number += 1
                try:
                    row = json.loads(line)
                    if row.get("schema_version") != 1:
                        raise ReplayFormatError("unsupported schema version")
                    yield int(row["event_time_ms"]), decode_event(
                        str(row["event_type"]),
                        row["payload"],
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ReplayFormatError(f"Invalid replay line {line_number}") from exc

    async def play(
        self,
        handler: Callable[[MarketEvent], Awaitable[None]],
        *,
        speed: float = 0.0,
    ) -> int:
        previous_time: int | None = None
        count = 0
        async for timestamp_ms, event in self.events():
            if speed > 0 and previous_time is not None:
                delay = max(timestamp_ms - previous_time, 0) / 1_000 / speed
                if delay:
                    await asyncio.sleep(delay)
            await handler(event)
            previous_time = timestamp_ms
            count += 1
        return count

    def sha256(self) -> str:
        digest = hashlib.sha256()
        with self._path.open("rb") as source:
            for chunk in iter(lambda: source.read(1_048_576), b""):
                digest.update(chunk)
        return digest.hexdigest()


def decode_event(event_type: str, payload: dict[str, Any]) -> MarketEvent:
    constructors: dict[str, Callable[[dict[str, Any]], MarketEvent]] = {
        "KlineEvent": lambda value: KlineEvent(Kline(**value)),
        "BookTickerEvent": lambda value: BookTickerEvent(BookTicker(**value)),
        "TickerEvent": lambda value: TickerEvent(Ticker24h(**value)),
        "MarkPriceEvent": lambda value: MarkPriceEvent(MarkPrice(**value)),
        "AggTradeEvent": lambda value: AggTradeEvent(AggTrade(**value)),
        "DepthEvent": _depth_event,
    }
    constructor = constructors.get(event_type)
    if constructor is None:
        raise ReplayFormatError(f"Unknown event type: {event_type}")
    return constructor(payload)


def _depth_event(payload: dict[str, Any]) -> DepthEvent:
    normalized = dict(payload)
    normalized["bids"] = tuple(tuple(row) for row in payload.get("bids", ()))
    normalized["asks"] = tuple(tuple(row) for row in payload.get("asks", ()))
    return DepthEvent(DepthSnapshot(**normalized))
