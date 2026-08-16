"""Queued rotating market recorder. / Очередь и ротация записи рынка."""

from __future__ import annotations

import asyncio
import dataclasses
import gzip
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiofiles
import structlog
from aiofiles.threadpool.text import AsyncTextIOWrapper

from pumplens.domain.events import BookTickerEvent, MarketEvent

log = structlog.get_logger(__name__)


class MarketEventRecorder:
    """Move JSON and disk work off the scanner path. / Убирает JSON и диск из scanner path."""

    def __init__(
        self,
        path: Path,
        *,
        queue_size: int = 50_000,
        batch_size: int = 500,
        flush_interval_seconds: float = 0.5,
        max_file_size_bytes: int = 200 * 1024 * 1024,
        retention_days: int = 10,
        gzip_rotated: bool = True,
        record_book_ticker: bool = True,
    ) -> None:
        self._path = path
        self._queue: asyncio.Queue[MarketEvent | None] = asyncio.Queue(maxsize=queue_size)
        self._batch_size = batch_size
        self._flush_interval_seconds = flush_interval_seconds
        self._max_file_size_bytes = max_file_size_bytes
        self._retention_days = retention_days
        self._gzip_rotated = gzip_rotated
        self._record_book_ticker = record_book_ticker
        self._file: AsyncTextIOWrapper | None = None
        self._worker: asyncio.Task[None] | None = None
        self._compression_tasks: set[asyncio.Task[None]] = set()
        self._current_size = 0
        self._last_flush_at = 0.0
        self._rotation_sequence = 0
        self.dropped_events = 0

    async def open(self) -> None:
        if self._worker is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        await self._remove_expired_files()
        await self._open_file()
        self._worker = asyncio.create_task(self._write_loop(), name="market-recorder-writer")

    async def close(self) -> None:
        worker = self._worker
        if worker is None:
            return
        await self._queue.put(None)
        await worker
        self._worker = None
        if self._compression_tasks:
            await asyncio.gather(*self._compression_tasks)
        if self._file is not None:
            await self._file.flush()
            await self._file.close()
            self._file = None

    async def record(self, event: MarketEvent) -> None:
        """Enqueue without awaiting disk. / Ставит в очередь без ожидания диска."""

        if self._worker is None:
            raise RuntimeError("Recorder is not open / Рекордер не открыт")
        if isinstance(event, BookTickerEvent) and not self._record_book_ticker:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_events += 1
            if self.dropped_events == 1 or self.dropped_events % 1_000 == 0:
                log.warning("market_recorder_queue_full", dropped=self.dropped_events)

    async def _write_loop(self) -> None:
        stopping = False
        while not stopping:
            event = await self._queue.get()
            if event is None:
                break
            batch = [event]
            # A live burst can fill the queue faster than a network volume accepts
            # small writes. Grow only the current batch; one writer still bounds all
            # disk and serialization work. / Во время всплеска увеличиваем только
            # текущий batch; запись и сериализация остаются строго последовательными.
            batch_limit = self._batch_size
            if self._batch_size > 1:
                batch_limit = max(
                    self._batch_size,
                    min(self._queue.qsize() + 1, self._batch_size * 20),
                )
            deadline = asyncio.get_running_loop().time() + self._flush_interval_seconds
            while len(batch) < batch_limit:
                try:
                    # Drain an active queue without creating one timeout task per event.
                    # Активную очередь забираем без отдельного timeout-task на событие.
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                    except TimeoutError:
                        break
                if item is None:
                    stopping = True
                    break
                batch.append(item)
            await self._write_batch(batch)

    async def _write_batch(self, events: list[MarketEvent]) -> None:
        # A single awaited worker keeps large burst serialization off the event loop
        # without creating an unbounded thread queue. / Один ожидаемый worker не
        # блокирует event loop и не создаёт бесконтрольную очередь потоков.
        serialized = await asyncio.to_thread(_serialize_batch, events)
        size = len(serialized.encode("utf-8"))
        if self._current_size and self._current_size + size > self._max_file_size_bytes:
            await self._rotate()
        if self._file is None:
            raise RuntimeError("Recorder file is closed / Файл рекордера закрыт")
        await self._file.write(serialized)
        now = asyncio.get_running_loop().time()
        if now - self._last_flush_at >= self._flush_interval_seconds:
            await self._file.flush()
            self._last_flush_at = now
        self._current_size += size

    async def _open_file(self) -> None:
        self._file = await aiofiles.open(self._path, "a", encoding="utf-8")
        self._current_size = self._path.stat().st_size if self._path.exists() else 0
        self._last_flush_at = asyncio.get_running_loop().time()

    async def _rotate(self) -> None:
        if self._file is not None:
            await self._file.flush()
            await self._file.close()
            self._file = None
        self._rotation_sequence += 1
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        rotated = self._path.with_name(
            f"{self._path.stem}-{stamp}-{self._rotation_sequence:04d}{self._path.suffix}"
        )
        self._path.replace(rotated)
        if self._gzip_rotated:
            task = asyncio.create_task(asyncio.to_thread(_gzip_and_remove, rotated))
            self._compression_tasks.add(task)
            task.add_done_callback(self._compression_tasks.discard)
        await self._remove_expired_files()
        await self._open_file()

    async def _remove_expired_files(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=self._retention_days)
        pattern = f"{self._path.stem}-*{self._path.suffix}*"
        for candidate in self._path.parent.glob(pattern):
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC)
            if modified < cutoff:
                await asyncio.to_thread(candidate.unlink, missing_ok=True)


def _serialize(event: MarketEvent) -> str:
    row = {
        "schema_version": 1,
        "event_type": type(event).__name__,
        "event_time_ms": event_time_ms(event),
        "payload": dataclasses.asdict(event.value),
    }
    return json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n"


def _serialize_batch(events: list[MarketEvent]) -> str:
    return "".join(_serialize(event) for event in events)


def _gzip_and_remove(path: Path) -> None:
    compressed = path.with_suffix(path.suffix + ".gz")
    with path.open("rb") as source, gzip.open(compressed, "wb") as target:
        shutil.copyfileobj(source, target)
    path.unlink(missing_ok=True)


def event_time_ms(event: MarketEvent) -> int:
    value: Any = event.value
    if hasattr(value, "event_time_ms"):
        return int(value.event_time_ms)
    if hasattr(value, "close_time_ms"):
        return int(value.close_time_ms)
    return 0
