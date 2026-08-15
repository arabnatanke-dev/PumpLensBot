"""Binance WebSocket routing tests. / Тесты маршрутизации WebSocket Binance."""

from pumplens.binance.normalizer import BinanceNormalizer
from pumplens.binance.ws_deep import DeepMarketWebSocket
from pumplens.binance.ws_market import MarketWebSocket, combined_stream_url
from pumplens.domain.events import MarketEvent


async def _ignore(_event: MarketEvent) -> None:
    return None


def test_stage_a_separates_market_and_public_streams() -> None:
    client = MarketWebSocket(
        "wss://fstream.binance.com",
        ["BTCUSDT", "ETHUSDT"],
        BinanceNormalizer(frozenset({"BTCUSDT", "ETHUSDT"})),
        _ignore,
    )
    market_streams = [stream for group in client._market_stream_groups() for stream in group]

    assert "!bookTicker" not in market_streams
    assert "!ticker@arr" in market_streams
    assert "!markPrice@arr@1s" in market_streams
    assert combined_stream_url(
        "wss://fstream.binance.com",
        "public",
        ("!bookTicker",),
    ) == "wss://fstream.binance.com/public/stream?streams=!bookTicker"


def test_stage_b_separates_trade_and_depth_routes() -> None:
    assert DeepMarketWebSocket.route_url(
        "wss://fstream.binance.com", "market"
    ) == "wss://fstream.binance.com/market/ws"
    assert DeepMarketWebSocket.route_url(
        "wss://fstream.binance.com", "public"
    ) == "wss://fstream.binance.com/public/ws"
    assert DeepMarketWebSocket._streams({"BTCUSDT"}, "aggTrade") == ["btcusdt@aggTrade"]
    assert DeepMarketWebSocket._streams({"BTCUSDT"}, "depth20@500ms") == [
        "btcusdt@depth20@500ms"
    ]
