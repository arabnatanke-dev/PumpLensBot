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
        self._base_url = base_url.rstrip("/")
        self._normalizer = normalizer
        self._handler = handler
        self._reconnect_max_seconds = reconnect_max_seconds
        self._desired_symbols: frozenset[str] = frozenset()
        self._changed = {"market": asyncio.Event(), "public": asyncio.Event()}
        self._request_ids = {"market": 0, "public": 0}

    async def set_symbols(self, symbols: Iterable[str]) -> None:
        desired = frozenset(symbols)
        if desired != self._desired_symbols:
            self._desired_symbols = desired
            for changed in self._changed.values():
                changed.set()

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._run_route("market", "aggTrade"), name="deep-market-ws")
            group.create_task(
                self._run_route("public", "depth20@500ms"),
                name="deep-public-ws",
            )

    async def _run_route(self, route: str, stream_suffix: str) -> None:
        attempt = 0
        while True:
            try:
                await self._run_connection(route, stream_suffix)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = min(2**attempt, self._reconnect_max_seconds)
                delay *= random.uniform(0.8, 1.2)
                log.warning(
                    "deep_ws_reconnect",
                    route=route,
                    delay_seconds=round(delay, 2),
                    error=type(exc).__name__,
                )
                await asyncio.sleep(delay)
                attempt = min(attempt + 1, 10)

    async def _run_connection(self, route: str, stream_suffix: str) -> None:
        url = self.route_url(self._base_url, route)
        log.info("deep_ws_connecting", route=route)
        async with websockets.connect(
            url,
            open_timeout=20,
            close_timeout=10,
            ping_interval=150,
            ping_timeout=600,
            max_queue=4_096,
        ) as websocket:
            log.info("deep_ws_connected", route=route)
            self._changed[route].set()
            async with asyncio.TaskGroup() as group:
                group.create_task(self._receive(websocket))
                group.create_task(
                    self._sync_subscriptions(websocket, route, stream_suffix)
                )

    async def _receive(self, websocket: ClientConnection) -> None:
        async for raw in websocket:
            payload: Any = json.loads(raw)
            for event in self._normalizer.normalize(payload):
                await self._handler(event)

    async def _sync_subscriptions(
        self,
        websocket: ClientConnection,
        route: str,
        stream_suffix: str,
    ) -> None:
        subscribed: frozenset[str] = frozenset()
        changed = self._changed[route]
        while True:
            await changed.wait()
            changed.clear()
            desired = self._desired_symbols
            additions = desired - subscribed
            removals = subscribed - desired
            if additions:
                await self._send(
                    websocket,
                    route,
                    "SUBSCRIBE",
                    self._streams(additions, stream_suffix),
                )
            if removals:
                await self._send(
                    websocket,
                    route,
                    "UNSUBSCRIBE",
                    self._streams(removals, stream_suffix),
                )
            subscribed = desired

    async def _send(
        self,
        websocket: ClientConnection,
        route: str,
        method: str,
        streams: list[str],
    ) -> None:
        # One control message may carry many params and stays below 10 msg/s.
        # Одно управляющее сообщение содержит много params и не превышает 10 msg/s.
        self._request_ids[route] += 1
        await websocket.send(
            json.dumps(
                {"method": method, "params": streams, "id": self._request_ids[route]}
            )
        )
        await asyncio.sleep(0.12)

    @staticmethod
    def _streams(symbols: Iterable[str], stream_suffix: str) -> list[str]:
        return [f"{symbol.lower()}@{stream_suffix}" for symbol in sorted(symbols)]

    @staticmethod
    def route_url(base_url: str, route: str) -> str:
        """Build a routed raw-stream URL. / Формирует routed raw-stream URL."""

        if route not in {"market", "public"}:
            raise ValueError(f"Unsupported Binance WebSocket route: {route}")
        return f"{base_url.rstrip('/')}/{route}/ws"
