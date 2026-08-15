"""Normalized JSONL market-event recorder. / JSONL-рекордер нормализованных событий."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

import aiofiles
from aiofiles.threadpool.text import AsyncTextIOWrapper

from pumplens.domain.events import MarketEvent


class MarketEventRecorder:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: AsyncTextIOWrapper | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = await aiofiles.open(self._path, "w", encoding="utf-8")

    async def close(self) -> None:
        if self._file is not None:
            await self._file.flush()
            await self._file.close()
            self._file = None

    async def record(self, event: MarketEvent) -> None:
        if self._file is None:
            raise RuntimeError("Recorder is not open / Рекордер не открыт")
        row = {
            "schema_version": 1,
            "event_type": type(event).__name__,
            "event_time_ms": event_time_ms(event),
            "payload": dataclasses.asdict(event.value),
        }
        serialized = json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n"
        async with self._lock:
            await self._file.write(serialized)


def event_time_ms(event: MarketEvent) -> int:
    value: Any = event.value
    if hasattr(value, "event_time_ms"):
        return int(value.event_time_ms)
    if hasattr(value, "close_time_ms"):
        return int(value.close_time_ms)
    return 0
