"""Public USD-M Futures REST client. / Клиент публичного REST USD-M Futures."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import httpx
import structlog

from pumplens.binance.rate_limiter import AsyncTokenBucket
from pumplens.domain.models import Kline, OpenInterestPoint, SymbolSpec, Ticker24h

log = structlog.get_logger(__name__)


class BinancePublicError(RuntimeError):
    """Raised after safe retries are exhausted. / Ошибка после исчерпания повторов."""


class BinancePublicClient:
    """Small typed client; public endpoints need no API key. / Публичный клиент без ключа."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 15.0,
        limiter: AsyncTokenBucket | None = None,
    ) -> None:
        self._limiter = limiter or AsyncTokenBucket()
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": "PumpLens/0.1 public-market-scanner"},
        )

    async def __aenter__(self) -> BinancePublicClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        weight: float = 1.0,
        attempts: int = 5,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(attempts):
            await self._limiter.acquire(weight)
            try:
                response = await self._client.get(path, params=params)
                if response.status_code == 429:
                    # Honor the server instruction instead of hammering the endpoint.
                    # Соблюдаем Retry-After и не создаём лавину повторных запросов.
                    retry_after = float(response.headers.get("Retry-After", "1"))
                    await asyncio.sleep(min(max(retry_after, 0.1), 60.0))
                    continue
                if response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(2**attempt, 8))

        raise BinancePublicError(f"GET {path} failed after {attempts} attempts") from last_error

    async def server_time_ms(self) -> int:
        payload = await self._get("/fapi/v1/time")
        return int(payload["serverTime"])

    async def exchange_info(self) -> list[SymbolSpec]:
        payload = await self._get("/fapi/v1/exchangeInfo", weight=1)
        return [self._parse_symbol(row) for row in payload.get("symbols", [])]

    async def tickers_24h(self) -> dict[str, Ticker24h]:
        # Requesting all symbols has a higher documented weight than one symbol.
        # Запрос всех тикеров имеет больший документированный вес.
        payload = await self._get("/fapi/v1/ticker/24hr", weight=40)
        return {
            str(row["symbol"]): Ticker24h(
                symbol=str(row["symbol"]),
                last_price=float(row["lastPrice"]),
                quote_volume=float(row["quoteVolume"]),
                price_change_pct=float(row["priceChangePercent"]),
                event_time_ms=int(row.get("closeTime", 0)),
            )
            for row in payload
        }

    async def klines(
        self,
        symbol: str,
        limit: int = 120,
        *,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> list[Kline]:
        params: dict[str, str | int] = {
            "symbol": symbol,
            "interval": "1m",
            "limit": limit,
        }
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        payload = await self._get(
            "/fapi/v1/klines",
            params=params,
            weight=1 if limit < 100 else 2,
        )
        now_ms = int(time.time() * 1_000)
        return [
            Kline(
                symbol=symbol,
                open_time_ms=int(row[0]),
                close_time_ms=int(row[6]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                base_volume=float(row[5]),
                quote_volume=float(row[7]),
                trade_count=int(row[8]),
                taker_buy_quote_volume=float(row[10]),
                # The final REST candle may still be open.
                # Последняя REST-свеча может быть ещё не закрыта.
                closed=int(row[6]) < now_ms,
            )
            for row in payload
        ]

    async def open_interest(self, symbol: str) -> OpenInterestPoint:
        payload = await self._get(
            "/fapi/v1/openInterest",
            params={"symbol": symbol},
            weight=1,
        )
        return OpenInterestPoint(
            symbol=str(payload["symbol"]),
            open_interest=float(payload["openInterest"]),
            timestamp_ms=int(payload["time"]),
        )

    async def ticker_price(self, symbol: str) -> float:
        payload = await self._get(
            "/fapi/v2/ticker/price",
            params={"symbol": symbol},
            weight=1,
        )
        if not isinstance(payload, Mapping) or "price" not in payload:
            raise BinancePublicError("ticker price response is invalid")
        return float(payload["price"])

    async def ticker_prices(self) -> dict[str, float]:
        """Fetch all latest prices in one public request. / Получает все цены одним запросом."""

        # Binance USD-M Symbol Price Ticker V2 returns an array when symbol is omitted.
        # Binance USD-M Symbol Price Ticker V2 без symbol возвращает массив цен.
        payload = await self._get("/fapi/v2/ticker/price", weight=2)
        if not isinstance(payload, list):
            raise BinancePublicError("all-symbol ticker price response is invalid")
        return {
            str(row["symbol"]): float(row["price"])
            for row in payload
            if isinstance(row, Mapping) and "symbol" in row and "price" in row
        }

    @staticmethod
    def _parse_symbol(row: Mapping[str, Any]) -> SymbolSpec:
        filters = tuple(row.get("filters", ()))
        by_type = {str(item.get("filterType")): item for item in filters}
        price_filter = by_type.get("PRICE_FILTER", {})
        lot_filter = by_type.get("LOT_SIZE", {})
        return SymbolSpec(
            symbol=str(row["symbol"]),
            status=str(row.get("status", "")),
            contract_type=str(row.get("contractType", "")),
            quote_asset=str(row.get("quoteAsset", "")),
            base_asset=str(row.get("baseAsset", "")),
            price_precision=int(row.get("pricePrecision", 8)),
            quantity_precision=int(row.get("quantityPrecision", 8)),
            tick_size=_optional_float(price_filter.get("tickSize")),
            step_size=_optional_float(lot_filter.get("stepSize")),
            min_quantity=_optional_float(lot_filter.get("minQty")),
            raw_filters=filters,
        )


def _optional_float(value: object) -> float | None:
    return float(str(value)) if value not in (None, "") else None
