"""One-symbol reuse of PumpLens scoring and Stage C. / Анализ одной монеты через PumpLens."""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any

from pumplens.analytics.buffers import MarketState
from pumplens.analytics.features import FeatureEngine, candle_structure
from pumplens.analytics.scoring import score_stage_a
from pumplens.analytics.stage_b import score_stage_b
from pumplens.analytics.stage_c import DeepEntryValidator
from pumplens.config import AppSettings
from pumplens.domain.enums import DataQuality, Direction, EntryDecision
from pumplens.domain.models import Candidate, EntryAnalysis, FeatureSnapshot, PriceZone
from pumplens.personal_monitor.domain import FullAnalysisResult, ScenarioPlan, SetupType

VALID_DECISIONS = {
    EntryDecision.ENTER_CANDIDATE,
    EntryDecision.WAIT_RETEST,
    EntryDecision.WATCH,
}


class PersonalSymbolAnalyzer:
    """Calculate LONG and SHORT independently from one bounded cache. / Считает оба направления."""

    def __init__(self, state: MarketState, settings: AppSettings) -> None:
        self._state = state
        self._settings = settings
        self._features = FeatureEngine(
            state,
            settings.binance.stale_after_seconds,
            max(settings.binance.stale_after_seconds, float(settings.oi.poll_seconds * 2)),
        )
        self._stage_c = DeepEntryValidator(state, settings.stage_c)

    def analyze(self, symbol: str) -> FullAnalysisResult:
        base = self._features.snapshot_all()[0]
        buffer = self._state.get(symbol)
        last_bar = (
            buffer.current_kline
            if buffer is not None and buffer.current_kline is not None
            else buffer.closed_klines[-1]
            if buffer is not None and buffer.closed_klines
            else None
        )
        directional: dict[Direction, tuple[FeatureSnapshot, EntryAnalysis | None]] = {}
        for direction in Direction:
            structure = candle_structure(last_bar, direction) if last_bar is not None else 0.0
            snapshot = replace(base, direction=direction, candle_structure=structure)
            snapshot = score_stage_b(
                score_stage_a(snapshot, self._settings.scanner.max_spread_pct),
                self._settings.scanner.max_spread_pct,
            )
            hard_reject = self._hard_reject(snapshot)
            candidate = Candidate(
                snapshot=snapshot,
                selected=hard_reject is None,
                hard_reject_reason=hard_reject,
            )
            validated = self._stage_c.validate([candidate])
            analysis = validated[0].snapshot.entry_analysis if validated else None
            directional[direction] = (snapshot, analysis)

        long_snapshot, long_analysis = directional[Direction.LONG]
        short_snapshot, short_analysis = directional[Direction.SHORT]
        plan = self._select_plan(directional)
        return FullAnalysisResult(
            symbol=symbol,
            price=base.last_price,
            long_score=long_snapshot.score,
            short_score=short_snapshot.score,
            long_snapshot=long_snapshot,
            short_snapshot=short_snapshot,
            long_analysis=long_analysis,
            short_analysis=short_analysis,
            plan=plan,
            data_quality=base.data_quality.value,
        )

    def _hard_reject(self, snapshot: FeatureSnapshot) -> str | None:
        if snapshot.data_quality is not DataQuality.FRESH:
            return snapshot.data_quality.value.lower()
        if snapshot.quote_volume_24h < self._settings.scanner.min_quote_volume_24h:
            return "low_24h_volume"
        if snapshot.spread_pct > self._settings.scanner.hard_reject_spread_pct:
            return "spread_too_wide"
        if (
            abs(snapshot.return_5m) >= self._settings.late.return_5m_pct
            or abs(snapshot.return_15m) >= self._settings.late.return_15m_pct
            or snapshot.range_pct_1m >= self._settings.late.range_1m_pct
        ):
            return "too_late"
        return None

    def _select_plan(
        self,
        directional: dict[Direction, tuple[FeatureSnapshot, EntryAnalysis | None]],
    ) -> ScenarioPlan | None:
        eligible: list[tuple[FeatureSnapshot, EntryAnalysis]] = []
        for snapshot, analysis in directional.values():
            if (
                analysis is not None
                and analysis.final_decision in VALID_DECISIONS
                and snapshot.score >= self._settings.scanner.candidate_score
                and analysis.entry_quality >= self._settings.stage_c.min_entry_quality
            ):
                eligible.append((snapshot, analysis))
        if not eligible:
            return None
        snapshot, analysis = max(
            eligible,
            key=lambda item: (item[1].entry_quality, item[0].score),
        )
        setup, zone, trigger = _setup_levels(snapshot.direction, analysis)
        return ScenarioPlan(
            direction=snapshot.direction.value,
            setup_type=setup,
            trigger_level=trigger,
            retest_zone=zone,
            invalidation_level=analysis.invalidation_price,
            target1=analysis.potential_target,
            require_oi_confirmation=snapshot.oi_data_ready,
            breakout_observed=analysis.breakout_detected,
            analysis=analysis,
            snapshot=snapshot,
        )


def _setup_levels(
    direction: Direction,
    analysis: EntryAnalysis,
) -> tuple[SetupType, PriceZone | None, float]:
    if analysis.breakout_level is not None:
        trigger = (
            analysis.breakout_level.high
            if direction is Direction.LONG
            else analysis.breakout_level.low
        )
        setup = (
            SetupType.BREAK_RETEST
            if analysis.final_decision is EntryDecision.WAIT_RETEST
            or analysis.retest_started
            else SetupType.BREAKOUT
        )
        return setup, analysis.breakout_level, trigger
    zone = (
        analysis.nearest_support
        if direction is Direction.LONG
        else analysis.nearest_resistance
    )
    setup = (
        SetupType.SUPPORT_BOUNCE
        if direction is Direction.LONG
        else SetupType.RESISTANCE_REJECTION
    )
    return setup, zone, analysis.entry_reference


def analysis_payload(result: FullAnalysisResult) -> dict[str, Any]:
    """Store a deterministic audit snapshot. / Сохраняет детерминированный audit snapshot."""

    return {
        "symbol": result.symbol,
        "price": result.price,
        "data_quality": result.data_quality,
        "long_score": result.long_score,
        "short_score": result.short_score,
        "long_snapshot": _snapshot_payload(result.long_snapshot),
        "short_snapshot": _snapshot_payload(result.short_snapshot),
        "long_analysis": asdict(result.long_analysis) if result.long_analysis else None,
        "short_analysis": asdict(result.short_analysis) if result.short_analysis else None,
        "selected_direction": result.plan.direction if result.plan else None,
        "selected_setup": result.plan.setup_type.value if result.plan else None,
    }


def _snapshot_payload(snapshot: FeatureSnapshot) -> dict[str, Any]:
    payload = asdict(snapshot)
    payload["timestamp"] = snapshot.timestamp.isoformat()
    payload["direction"] = snapshot.direction.value
    payload["data_quality"] = snapshot.data_quality.value
    # Stage C is stored separately above and must not be duplicated recursively.
    # Stage C хранится отдельно выше и не должен дублироваться рекурсивно.
    payload["entry_analysis"] = None
    return payload
