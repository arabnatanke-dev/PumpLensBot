"""Live Binance routed WebSocket checks. / Живые проверки routed WebSocket Binance."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
import websockets

from pumplens.binance.ws_market import combined_stream_url

pytestmark = pytest.mark.integration


async def _observed_streams(url: str, expected: set[str]) -> set[str]:
    """Wait for every requested live stream. / Ждёт каждый запрошенный живой поток."""

    observed: set[str] = set()
    async with websockets.connect(url, open_timeout=15, close_timeout=5) as websocket:
        async with asyncio.timeout(30):
            while not expected <= observed:
                payload = json.loads(await websocket.recv())
                stream = payload.get("stream") if isinstance(payload, dict) else None
                if isinstance(stream, str):
                    observed.add(stream)
    return observed


async def test_binance_market_and_public_routes_deliver_all_required_streams() -> None:
    if os.environ.get("RUN_BINANCE_WS_SMOKE") != "1":
        pytest.skip("set RUN_BINANCE_WS_SMOKE=1 to use live Binance")

    base_url = "wss://fstream.binance.com"
    market = {
        "btcusdt@kline_1m",
        "btcusdt@ticker",
        "btcusdt@markPrice@1s",
        "btcusdt@aggTrade",
    }
    public = {"btcusdt@bookTicker", "btcusdt@depth20@500ms"}

    market_seen, public_seen = await asyncio.gather(
        _observed_streams(combined_stream_url(base_url, "market", sorted(market)), market),
        _observed_streams(combined_stream_url(base_url, "public", sorted(public)), public),
    )
    assert market <= market_seen
    assert public <= public_seen
