"""Dynamic single-symbol market streams. / Динамические потоки персонального монитора."""

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


class PersonalMarketWebSocket:
    """Subscribe only user-selected symbols. / Подписывает только выбранные монеты."""

    _MARKET_SUFFIXES = ("kline_1m", "ticker", "markPrice@1s", "aggTrade")
    _PUBLIC_SUFFIXES = ("bookTicker", "depth20@500ms")

    def __init__(
        self,
        base_url: str,
        handler: EventHandler,
        *,
        reconnect_max_seconds: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._handler = handler
        self._reconnect_max_seconds = reconnect_max_seconds
        self._desired_symbols: frozenset[str] = frozenset()
        self._normalizer = BinanceNormalizer(self._desired_symbols)
        self._changed = {"market": asyncio.Event(), "public": asyncio.Event()}
        self._request_ids = {"market": 0, "public": 0}

    @property
    def desired_symbols(self) -> frozenset[str]:
        return self._desired_symbols

    async def set_symbols(self, symbols: Iterable[str]) -> None:
        desired = frozenset(symbol.upper() for symbol in symbols)
        if desired == self._desired_symbols:
            return
        self._desired_symbols = desired
        # Assignment is atomic on the event loop; no partially updated filter is visible.
        # Присваивание атомарно для event loop; частично обновлённый фильтр не виден.
        self._normalizer = BinanceNormalizer(desired)
        for changed in self._changed.values():
            changed.set()

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._run_route("market", self._MARKET_SUFFIXES))
            group.create_task(self._run_route("public", self._PUBLIC_SUFFIXES))

    async def _run_route(self, route: str, suffixes: tuple[str, ...]) -> None:
        attempt = 0
        while True:
            try:
                await self._run_connection(route, suffixes)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = min(2**attempt, self._reconnect_max_seconds)
                delay *= random.uniform(0.8, 1.2)
                log.warning(
                    "personal_ws_reconnect",
                    route=route,
                    delay_seconds=round(delay, 2),
                    error=type(exc).__name__,
                )
                await asyncio.sleep(delay)
                attempt = min(attempt + 1, 10)

    async def _run_connection(self, route: str, suffixes: tuple[str, ...]) -> None:
        url = self.route_url(self._base_url, route)
        async with websockets.connect(
            url,
            open_timeout=20,
            close_timeout=10,
            ping_interval=150,
            ping_timeout=600,
            max_queue=2_048,
        ) as websocket:
            log.info("personal_ws_connected", route=route)
            self._changed[route].set()
            async with asyncio.TaskGroup() as group:
                group.create_task(self._receive(websocket))
                group.create_task(self._sync_subscriptions(websocket, route, suffixes))

    async def _receive(self, websocket: ClientConnection) -> None:
        async for raw in websocket:
            payload: Any = json.loads(raw)
            for event in self._normalizer.normalize(payload):
                await self._handler(event)

    async def _sync_subscriptions(
        self,
        websocket: ClientConnection,
        route: str,
        suffixes: tuple[str, ...],
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
                await self._send(websocket, route, "SUBSCRIBE", self._streams(additions, suffixes))
            if removals:
                await self._send(
                    websocket,
                    route,
                    "UNSUBSCRIBE",
                    self._streams(removals, suffixes),
                )
            subscribed = desired

    async def _send(
        self,
        websocket: ClientConnection,
        route: str,
        method: str,
        streams: list[str],
    ) -> None:
        self._request_ids[route] += 1
        await websocket.send(
            json.dumps({"method": method, "params": streams, "id": self._request_ids[route]})
        )
        # Binance counts control frames; keep safely below the documented limit.
        # Binance считает control frames; оставляем безопасный запас по лимиту.
        await asyncio.sleep(0.12)

    @staticmethod
    def _streams(symbols: Iterable[str], suffixes: Iterable[str]) -> list[str]:
        return [
            f"{symbol.lower()}@{suffix}"
            for symbol in sorted(symbols)
            for suffix in suffixes
        ]

    @staticmethod
    def route_url(base_url: str, route: str) -> str:
        if route not in {"market", "public"}:
            raise ValueError(f"Unsupported Binance WebSocket route: {route}")
        return f"{base_url.rstrip('/')}/{route}/ws"
