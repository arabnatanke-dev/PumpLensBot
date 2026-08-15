"""Dynamic Stage B streams. / Динамические потоки глубокого анализа Stage B."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import structlog
import websockets
from websockets.asyncio.client import ClientConnection

from pumplens.binance.normalizer import BinanceNormalizer
from pumplens.domain.events import MarketEvent

log = structlog.get_logger(__name__)

EventHandler = Callable[[MarketEvent], Awaitable[None]]


class DeepMarketWebSocket:
    """Subscribe only active candidates to expensive streams. / Подписывает кандидатов."""

    def __init__(
        self,
        base_url: str,
        normalizer: BinanceNormalizer,
        handler: EventHandler,
        *,
        reconnect_max_seconds: float = 30.0,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/ws"
        self._normalizer = normalizer
        self._handler = handler
        self._reconnect_max_seconds = reconnect_max_seconds
        self._desired_symbols: frozenset[str] = frozenset()
        self._changed = asyncio.Event()
        self._request_id = 0

    async def set_symbols(self, symbols: Iterable[str]) -> None:
        desired = frozenset(symbols)
        if desired != self._desired_symbols:
            self._desired_symbols = desired
            self._changed.set()

    async def run(self) -> None:
        attempt = 0
        while True:
            try:
                await self._run_connection()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = min(2**attempt, self._reconnect_max_seconds)
                delay *= random.uniform(0.8, 1.2)
                log.warning(
                    "deep_ws_reconnect",
                    delay_seconds=round(delay, 2),
                    error=type(exc).__name__,
                )
                await asyncio.sleep(delay)
                attempt = min(attempt + 1, 10)

    async def _run_connection(self) -> None:
        log.info("deep_ws_connecting")
        async with websockets.connect(
            self._url,
            open_timeout=20,
            close_timeout=10,
            ping_interval=150,
            ping_timeout=600,
            max_queue=4_096,
        ) as websocket:
            log.info("deep_ws_connected")
            self._changed.set()
            async with asyncio.TaskGroup() as group:
                group.create_task(self._receive(websocket))
                group.create_task(self._sync_subscriptions(websocket))

    async def _receive(self, websocket: ClientConnection) -> None:
        async for raw in websocket:
            payload: Any = json.loads(raw)
            for event in self._normalizer.normalize(payload):
                await self._handler(event)

    async def _sync_subscriptions(self, websocket: ClientConnection) -> None:
        subscribed: frozenset[str] = frozenset()
        while True:
            await self._changed.wait()
            self._changed.clear()
            desired = self._desired_symbols
            additions = desired - subscribed
            removals = subscribed - desired
            if additions:
                await self._send(websocket, "SUBSCRIBE", self._streams(additions))
            if removals:
                await self._send(websocket, "UNSUBSCRIBE", self._streams(removals))
            subscribed = desired

    async def _send(
        self,
        websocket: ClientConnection,
        method: str,
        streams: list[str],
    ) -> None:
        # One control message may carry many params and stays below 10 msg/s.
        # Одно управляющее сообщение содержит много params и не превышает 10 msg/s.
        self._request_id += 1
        await websocket.send(
            json.dumps({"method": method, "params": streams, "id": self._request_id})
        )
        await asyncio.sleep(0.12)

    @staticmethod
    def _streams(symbols: Iterable[str]) -> list[str]:
        streams: list[str] = []
        for symbol in sorted(symbols):
            lower = symbol.lower()
            # The current USD-M raw trade stream is live and is aggregated locally.
            # Текущий raw trade USD-M работает; агрегируем сделки локально по секундам.
            streams.extend((f"{lower}@trade", f"{lower}@depth20@500ms"))
        return streams
