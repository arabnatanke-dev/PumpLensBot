"""Cheap all-market candidate selection. / Дешёвый отбор кандидатов по всему рынку."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta

from pumplens.analytics.features import FeatureEngine
from pumplens.analytics.scoring import score_stage_a
from pumplens.config import EarlyLiquidityTier, EarlySettings, LateSettings, ScannerSettings
from pumplens.domain.enums import DataQuality, Direction
from pumplens.domain.models import Candidate, FeatureSnapshot


class StageAScanner:
    def __init__(
        self,
        feature_engine: FeatureEngine,
        settings: ScannerSettings,
        late: LateSettings,
        early: EarlySettings | None = None,
    ) -> None:
        self._feature_engine = feature_engine
        self._settings = settings
        self._late = late
        self._early = early or EarlySettings()
        self._hits: dict[str, deque[bool]] = defaultdict(lambda: deque(maxlen=3))
        self._early_hits: dict[str, deque[tuple[datetime, bool]]] = defaultdict(deque)

    def scan_once(self) -> list[Candidate]:
        snapshots = [
            score_stage_a(snapshot, self._settings.max_spread_pct)
            for snapshot in self._feature_engine.snapshot_all()
        ]
        candidates = [self._evaluate(snapshot) for snapshot in snapshots]
        return sorted(candidates, key=lambda item: item.snapshot.score, reverse=True)

    def _evaluate(self, snapshot: FeatureSnapshot) -> Candidate:
        hard_reject = self._hard_reject(snapshot)
        sign = 1.0 if snapshot.direction is Direction.LONG else -1.0
        pressure = (
            snapshot.buy_pressure
            if snapshot.direction is Direction.LONG
            else 1.0 - snapshot.buy_pressure
        )
        confirmations: list[str] = []
        if sign * snapshot.return_1m >= 0.6 or sign * snapshot.return_3m >= 1.2:
            confirmations.append("price")
        if snapshot.volume_ratio_1m >= 2.5:
            confirmations.append("volume")
        if snapshot.trade_rate_ratio >= 2.0:
            confirmations.append("trade_rate")
        if pressure >= 0.58:
            confirmations.append("pressure")
        if sign * snapshot.relative_strength_1m >= 0.5:
            confirmations.append("relative_strength")

        current_hit = (
            hard_reject is None
            and snapshot.score >= self._settings.candidate_score
            and len(confirmations) >= 3
        )
        history = self._hits[snapshot.symbol]
        history.append(current_hit)
        selected = current_hit and sum(history) >= 2
        early_hit, liquidity_tier = evaluate_early(
            snapshot,
            hard_reject,
            self._settings,
            self._early,
        )
        early_selected = self._early_debounced(snapshot, early_hit)
        return Candidate(
            snapshot=snapshot,
            selected=selected,
            hard_reject_reason=hard_reject,
            confirmations=tuple(confirmations),
            early_selected=early_selected,
            early_liquidity_tier=liquidity_tier if early_selected else None,
            early_snapshot=snapshot if early_selected else None,
        )

    def _early_debounced(self, snapshot: FeatureSnapshot, current_hit: bool) -> bool:
        history = self._early_hits[snapshot.symbol]
        cutoff = snapshot.timestamp - timedelta(seconds=self._early.debounce_window_seconds)
        while history and history[0][0] <= cutoff:
            history.popleft()
        history.append((snapshot.timestamp, current_hit))
        return current_hit and sum(hit for _, hit in history) >= self._early.debounce_hits

    def _hard_reject(self, snapshot: FeatureSnapshot) -> str | None:
        if snapshot.data_quality is not DataQuality.FRESH:
            return snapshot.data_quality.value.lower()
        if snapshot.quote_volume_24h < self._settings.min_quote_volume_24h:
            return "low_24h_volume"
        if snapshot.spread_pct > self._settings.hard_reject_spread_pct:
            return "spread_too_wide"
        if (
            abs(snapshot.return_5m) >= self._late.return_5m_pct
            or abs(snapshot.return_15m) >= self._late.return_15m_pct
            or snapshot.range_pct_1m >= self._late.range_1m_pct
        ):
            return "too_late"
        return None


def evaluate_early(
    snapshot: FeatureSnapshot,
    hard_reject: str | None,
    scanner: ScannerSettings,
    early: EarlySettings,
) -> tuple[bool, str | None]:
    """Evaluate the strict flash gate before deep confirmation. / Проверяет ранний flash-gate."""

    tier = _liquidity_tier(snapshot.quote_volume_24h, early.liquidity_tiers)
    if not early.enabled or hard_reject is not None or tier is None:
        return False, None
    pressure = (
        snapshot.buy_pressure
        if snapshot.direction is Direction.LONG
        else 1.0 - snapshot.buy_pressure
    )
    quote_floor = max(early.min_quote_volume_1m, tier.min_quote_volume_1m)
    trade_floor = max(early.min_trade_count_1m, tier.min_trade_count_1m)
    one_minute_move = abs(snapshot.return_1m)
    return (
        snapshot.data_quality is DataQuality.FRESH
        and early.min_score <= snapshot.score <= early.max_score
        and snapshot.volume_ratio_1m >= early.min_volume_ratio
        and snapshot.trade_rate_ratio >= early.min_trade_rate_ratio
        and pressure >= early.min_pressure
        and snapshot.volume_robust_z >= early.min_volume_z
        and early.min_return_1m_pct <= one_minute_move <= early.max_return_1m_pct
        and abs(snapshot.return_5m) < early.max_return_5m_pct
        and snapshot.spread_pct <= min(scanner.max_spread_pct, early.max_spread_pct)
        and snapshot.quote_volume_1m >= quote_floor
        and snapshot.trade_count_1m >= trade_floor,
        tier.name,
    )


def _liquidity_tier(
    quote_volume_24h: float,
    tiers: tuple[EarlyLiquidityTier, ...],
) -> EarlyLiquidityTier | None:
    eligible = [tier for tier in tiers if quote_volume_24h >= tier.min_quote_volume_24h]
    return eligible[-1] if eligible else None
