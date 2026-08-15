"""Dynamic symbol universe. / Динамический список анализируемых символов."""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from pumplens.binance.public_rest import BinancePublicClient
from pumplens.domain.models import SymbolSpec, Ticker24h

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Universe:
    symbols: tuple[SymbolSpec, ...]
    tickers: dict[str, Ticker24h]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item.symbol for item in self.symbols)


class UniverseManager:
    def __init__(
        self,
        client: BinancePublicClient,
        min_quote_volume_24h: float,
        blacklist: frozenset[str] = frozenset(),
    ) -> None:
        self._client = client
        self._min_quote_volume_24h = min_quote_volume_24h
        self._blacklist = blacklist

    async def load(self) -> Universe:
        specs = await self._client.exchange_info()
        tickers = await self._client.tickers_24h()
        selected = tuple(
            sorted(
                (
                    spec
                    for spec in specs
                    if spec.status == "TRADING"
                    and spec.contract_type == "PERPETUAL"
                    and spec.quote_asset == "USDT"
                    and spec.symbol not in self._blacklist
                    and (ticker := tickers.get(spec.symbol)) is not None
                    and ticker.quote_volume >= self._min_quote_volume_24h
                ),
                key=lambda item: item.symbol,
            )
        )
        log.info(
            "universe_loaded",
            selected=len(selected),
            exchange_symbols=len(specs),
            min_quote_volume=self._min_quote_volume_24h,
        )
        return Universe(symbols=selected, tickers=tickers)
