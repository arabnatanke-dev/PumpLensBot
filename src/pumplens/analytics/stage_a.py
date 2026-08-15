"""Cheap all-market candidate selection. / Дешёвый отбор кандидатов по всему рынку."""

from __future__ import annotations

from collections import defaultdict, deque

from pumplens.analytics.features import FeatureEngine
from pumplens.analytics.scoring import score_stage_a
from pumplens.config import LateSettings, ScannerSettings
from pumplens.domain.enums import DataQuality, Direction
from pumplens.domain.models import Candidate, FeatureSnapshot


class StageAScanner:
    def __init__(
        self,
        feature_engine: FeatureEngine,
        settings: ScannerSettings,
        late: LateSettings,
    ) -> None:
        self._feature_engine = feature_engine
        self._settings = settings
        self._late = late
        self._hits: dict[str, deque[bool]] = defaultdict(lambda: deque(maxlen=3))

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
        return Candidate(
            snapshot=snapshot,
            selected=selected,
            hard_reject_reason=hard_reject,
            confirmations=tuple(confirmations),
        )

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
