"""Candidate retention, deep scoring, and OI polling. / Удержание и анализ Stage B."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace

import structlog

from pumplens.analytics.buffers import MarketState
from pumplens.analytics.scoring import clamp01, score_stage_a
from pumplens.binance.public_rest import BinancePublicClient
from pumplens.domain.enums import Direction
from pumplens.domain.models import Candidate, FeatureSnapshot

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class ActiveCandidate:
    symbol: str
    expires_at: float
    last_rank: int


class StageBManager:
    """Keep promoted symbols for at least 180 seconds. / Удерживает символы минимум 180 с."""

    def __init__(self, max_candidates: int = 30, retention_seconds: float = 180.0) -> None:
        self._max_candidates = max_candidates
        self._retention_seconds = retention_seconds
        self._active: dict[str, ActiveCandidate] = {}

    @property
    def active_symbols(self) -> tuple[str, ...]:
        return tuple(self._active)

    def update(self, ranked: Sequence[Candidate]) -> tuple[str, ...]:
        now = time.monotonic()
        self._active = {
            symbol: item for symbol, item in self._active.items() if item.expires_at > now
        }

        eligible = [
            candidate
            for candidate in ranked
            if candidate.selected and candidate.hard_reject_reason is None
        ]
        for rank, candidate in enumerate(eligible):
            symbol = candidate.snapshot.symbol
            current = self._active.get(symbol)
            expires_at = now + self._retention_seconds
            if current is None:
                self._active[symbol] = ActiveCandidate(symbol, expires_at, rank)
                log.info("stage_b_promoted", symbol=symbol, score=candidate.snapshot.score)
            else:
                current.expires_at = max(current.expires_at, expires_at)
                current.last_rank = rank

        if len(self._active) > self._max_candidates:
            keep = sorted(self._active.values(), key=lambda item: item.last_rank)[
                : self._max_candidates
            ]
            self._active = {item.symbol: item for item in keep}
        return self.active_symbols

    def rescore(self, ranked: Sequence[Candidate], max_spread_pct: float) -> list[Candidate]:
        by_symbol = {candidate.snapshot.symbol: candidate for candidate in ranked}
        result: list[Candidate] = []
        for symbol in self._active:
            candidate = by_symbol.get(symbol)
            if candidate is None:
                continue
            result.append(
                replace(candidate, snapshot=score_stage_b(candidate.snapshot, max_spread_pct))
            )
        return sorted(result, key=lambda item: item.snapshot.score, reverse=True)


def score_stage_b(snapshot: FeatureSnapshot, max_spread_pct: float) -> FeatureSnapshot:
    """Replace coarse flow data with aggTrade/depth/OI confirmation. / Глубокий score."""

    if not snapshot.deep_data_ready:
        return snapshot

    pressure = snapshot.agg_buy_pressure
    trade_ratio = snapshot.agg_trade_rate_ratio or snapshot.trade_rate_ratio
    deep_input = replace(snapshot, buy_pressure=pressure, trade_rate_ratio=trade_ratio)
    scored = score_stage_a(deep_input, max_spread_pct)
    sign = 1.0 if snapshot.direction is Direction.LONG else -1.0
    directional_oi = sign * snapshot.oi_delta_pct
    oi_points = 12.0 * clamp01(directional_oi / 1.0)
    directional_depth = sign * snapshot.depth_imbalance
    depth_points = 4.0 * clamp01(directional_depth / 0.35)

    reasons = list(scored.reasons)
    penalties = list(scored.penalties)
    if directional_oi > 0:
        reasons.append(f"OI {directional_oi:+.2f}%")
    elif directional_oi < -0.5:
        penalties.append("OI divergence / расхождение OI")
    if directional_depth > 0.1:
        reasons.append(f"depth {directional_depth:+.2f}")

    return replace(
        scored,
        score=round(min(scored.score + oi_points + depth_points, 100.0), 2),
        reasons=tuple(reasons),
        penalties=tuple(penalties),
    )


class OpenInterestPoller:
    def __init__(
        self,
        client: BinancePublicClient,
        state: MarketState,
        manager: StageBManager,
        interval_seconds: int = 30,
        concurrency: int = 8,
    ) -> None:
        self._client = client
        self._state = state
        self._manager = manager
        self._interval_seconds = interval_seconds
        self._semaphore = asyncio.Semaphore(concurrency)

    async def run(self) -> None:
        while True:
            symbols = self._manager.active_symbols
            if symbols:
                async with asyncio.TaskGroup() as group:
                    for symbol in symbols:
                        group.create_task(self._poll_one(symbol))
            await asyncio.sleep(self._interval_seconds)

    async def _poll_one(self, symbol: str) -> None:
        try:
            async with self._semaphore:
                point = await self._client.open_interest(symbol)
            self._state.record_open_interest(point)
        except Exception as exc:
            # One symbol must never break the market scanner.
            # Ошибка одного символа не должна останавливать общий сканер.
            log.warning("open_interest_failed", symbol=symbol, error=type(exc).__name__)
