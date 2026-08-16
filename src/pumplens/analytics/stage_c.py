"""Deep entry validation for a bounded Stage B shortlist. / Глубокая проверка shortlist."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, replace

import structlog

from pumplens.analytics.buffers import MarketState
from pumplens.analytics.market_structure import (
    LevelMap,
    aggregate_klines,
    analyze_timeframe,
    average_true_range,
    build_level_map,
    nearest_zones,
    opposing_wick_ratio,
    rolling_vwap,
)
from pumplens.config import StageCSettings
from pumplens.domain.enums import (
    BreakoutState,
    DataQuality,
    Direction,
    EntryDecision,
    MarketStructure,
    RetestState,
)
from pumplens.domain.models import (
    Candidate,
    EntryAnalysis,
    FeatureSnapshot,
    Kline,
    PriceZone,
    TimeframeStructure,
)

log = structlog.get_logger(__name__)
EPSILON = 1e-12


@dataclass(frozen=True, slots=True)
class PreparedStageCInput:
    candidate: Candidate
    closed_klines: tuple[Kline, ...]
    current_kline: Kline | None


class DeepEntryValidator:
    """Run local Stage C calculations only for top Stage B rows. / Только top-N Stage B."""

    def __init__(self, state: MarketState, settings: StageCSettings) -> None:
        self._state = state
        self._settings = settings
        self.last_latency_ms = 0.0
        self.last_processed_count = 0
        self._logged_signatures: dict[
            tuple[str, Direction],
            tuple[EntryDecision, BreakoutState, RetestState, int],
        ] = {}

    def validate(self, ranked: Sequence[Candidate]) -> list[Candidate]:
        return self.validate_prepared(self.prepare(ranked))

    def prepare(self, ranked: Sequence[Candidate]) -> list[PreparedStageCInput]:
        """Copy bounded state on the event loop. / Копирует bounded state в event loop."""

        if not self._settings.enabled:
            return []
        eligible = [
            candidate
            for candidate in ranked
            if candidate.hard_reject_reason is None
            and candidate.snapshot.data_quality is DataQuality.FRESH
        ][: self._settings.max_candidates]
        prepared: list[PreparedStageCInput] = []
        for candidate in eligible:
            buffer = self._state.get(candidate.snapshot.symbol)
            if buffer is None:
                continue
            prepared.append(
                PreparedStageCInput(
                    candidate=candidate,
                    closed_klines=tuple(buffer.closed_klines),
                    current_kline=buffer.current_kline,
                )
            )
        return prepared

    def validate_prepared(
        self,
        prepared: Sequence[PreparedStageCInput],
    ) -> list[Candidate]:
        """CPU-only validation safe for asyncio.to_thread. / CPU-анализ вне event loop."""

        started = time.perf_counter()
        self.last_processed_count = 0
        if not self._settings.enabled:
            self.last_latency_ms = 0.0
            return []
        validated: list[Candidate] = []
        for item in prepared:
            candidate = item.candidate
            candidate_started = time.perf_counter()
            analysis = self._analyze(
                candidate.snapshot,
                item.closed_klines,
                item.current_kline,
            )
            if analysis is None:
                continue
            snapshot = replace(candidate.snapshot, entry_analysis=analysis)
            hard_reject = candidate.hard_reject_reason
            if analysis.final_decision is EntryDecision.SKIP_LATE:
                hard_reject = "too_late"
            validated.append(
                replace(candidate, snapshot=snapshot, hard_reject_reason=hard_reject)
            )
            self.last_processed_count += 1
            self._log_changed_analysis(
                snapshot,
                analysis,
                (time.perf_counter() - candidate_started) * 1_000,
            )
        self.last_latency_ms = (time.perf_counter() - started) * 1_000
        return validated

    def _log_changed_analysis(
        self,
        snapshot: FeatureSnapshot,
        analysis: EntryAnalysis,
        calculation_ms: float,
    ) -> None:
        key = (snapshot.symbol, snapshot.direction)
        signature = (
            analysis.final_decision,
            analysis.breakout_state,
            analysis.retest_state,
            round(analysis.entry_quality / 5.0),
        )
        if self._logged_signatures.get(key) == signature:
            return
        self._logged_signatures[key] = signature
        log.info(
            "stage_c_analysis",
            symbol=snapshot.symbol,
            direction=snapshot.direction.value,
            signal_score=snapshot.score,
            entry_quality=analysis.entry_quality,
            structure=analysis.structure_5m.structure.value,
            breakout=analysis.breakout_state.value,
            retest=analysis.retest_state.value,
            late_score=analysis.late_score,
            exhaustion=analysis.exhaustion_score,
            rr=analysis.rr,
            decision=analysis.final_decision.value,
            stage_c_ms=round(calculation_ms, 3),
        )

    def _analyze(
        self,
        snapshot: FeatureSnapshot,
        closed_klines: Sequence[Kline],
        current_kline: Kline | None,
    ) -> EntryAnalysis | None:
        closed = list(closed_klines)[-self._settings.structure_lookback :]
        if len(closed) < 30:
            return None
        bars = list(closed)
        if (
            current_kline is not None
            and current_kline.open_time_ms > closed[-1].open_time_ms
        ):
            bars.append(current_kline)
        else:
            bars[-1] = replace(
                bars[-1],
                high=max(bars[-1].high, snapshot.last_price),
                low=min(bars[-1].low, snapshot.last_price),
                close=snapshot.last_price,
            )

        structure_1m = self._structure(bars, "1m")
        structure_5m = self._structure(aggregate_klines(bars, 5), "5m")
        structure_15m = self._structure(aggregate_klines(bars, 15), "15m")
        level_history = closed[-self._settings.level_lookback :]
        levels = build_level_map(
            level_history,
            pivot_window=self._settings.pivot_window,
            tolerance_pct=self._settings.level_cluster_tolerance_pct,
            min_touches=self._settings.level_min_touches,
        )
        support, resistance = nearest_zones(levels, snapshot.last_price)
        breakout_state, breakout_level = self._breakout(snapshot, bars, levels)
        retest_state = self._retest(snapshot.direction, bars, breakout_state, breakout_level)
        if breakout_level is not None:
            if snapshot.direction is Direction.LONG and breakout_level.high < snapshot.last_price:
                support = breakout_level
            elif (
                snapshot.direction is Direction.SHORT
                and breakout_level.low > snapshot.last_price
            ):
                resistance = breakout_level

        atr = average_true_range(bars, self._settings.atr_period)
        atr_pct = atr / max(snapshot.last_price, EPSILON) * 100.0
        vwap = rolling_vwap(bars)
        distance_from_vwap_pct = (
            abs(snapshot.last_price / vwap - 1.0) * 100.0 if vwap > 0 else 0.0
        )
        alignment = self._alignment(
            snapshot.direction,
            structure_1m,
            structure_5m,
            structure_15m,
        )
        late_score = self._late_score(
            snapshot,
            atr_pct,
            distance_from_vwap_pct,
            breakout_level,
            structure_1m,
        )
        exhaustion_score = self._exhaustion_score(
            snapshot,
            bars,
            atr_pct,
            distance_from_vwap_pct,
        )
        (
            entry_reference,
            invalidation,
            target,
            risk_pct,
            reward_pct,
            rr,
        ) = self._risk_model(snapshot, atr, support, resistance, breakout_level)
        distance_support = _distance_below(snapshot.last_price, support)
        distance_resistance = _distance_above(snapshot.last_price, resistance)
        directional_room = (
            distance_resistance
            if snapshot.direction is Direction.LONG
            else distance_support
        )
        quality, positive, negative, missing = self._entry_quality(
            snapshot=snapshot,
            alignment=alignment,
            breakout_state=breakout_state,
            retest_state=retest_state,
            room_pct=directional_room,
            rr=rr,
            late_score=late_score,
            exhaustion_score=exhaustion_score,
            vwap_distance_pct=distance_from_vwap_pct,
            opposing_wick=opposing_wick_ratio(bars[-1], snapshot.direction),
        )
        decision = select_entry_decision(
            self._settings,
            snapshot.direction,
            quality,
            breakout_state,
            retest_state,
            directional_room,
            rr,
            alignment,
            late_score,
            exhaustion_score,
        )
        reason_codes = tuple(dict.fromkeys((*positive, *negative, decision.value)))
        return EntryAnalysis(
            signal_score=snapshot.score,
            entry_quality=quality,
            structure_1m=structure_1m,
            structure_5m=structure_5m,
            structure_15m=structure_15m,
            trend_alignment_score=alignment,
            nearest_support=support,
            nearest_resistance=resistance,
            distance_to_support_pct=distance_support,
            distance_to_resistance_pct=distance_resistance,
            room_up_pct=distance_resistance,
            room_down_pct=distance_support,
            breakout_state=breakout_state,
            breakout_level=breakout_level,
            breakout_detected=breakout_state is not BreakoutState.NONE,
            retest_state=retest_state,
            retest_started=retest_state in {RetestState.STARTED, RetestState.HELD},
            retest_held=retest_state is RetestState.HELD,
            retest_failed=retest_state is RetestState.FAILED,
            atr_pct=round(atr_pct, 4),
            distance_from_vwap_pct=round(distance_from_vwap_pct, 4),
            late_score=late_score,
            late=late_score >= self._settings.late_score_threshold,
            exhaustion_score=exhaustion_score,
            entry_reference=entry_reference,
            invalidation_price=invalidation,
            potential_target=target,
            risk_pct=risk_pct,
            reward_pct=reward_pct,
            rr=rr,
            final_decision=decision,
            reason_codes=reason_codes,
            positive_reasons=positive,
            negative_reasons=negative,
            optional_data_missing=missing,
        )

    def _structure(self, bars: Sequence[Kline], timeframe: str) -> TimeframeStructure:
        return analyze_timeframe(
            bars,
            timeframe=timeframe,
            pivot_window=self._settings.pivot_window,
            compression_ratio=self._settings.compression_ratio,
            expansion_ratio=self._settings.expansion_ratio,
        )

    def _breakout(
        self,
        snapshot: FeatureSnapshot,
        bars: Sequence[Kline],
        levels: LevelMap,
    ) -> tuple[BreakoutState, PriceZone | None]:
        current = bars[-1]
        previous_close = bars[-2].close if len(bars) >= 2 else current.open
        tolerance = self._settings.retest_tolerance_pct / 100.0
        pressure = _directional_pressure(snapshot)
        flow_confirmed = (
            snapshot.volume_ratio_1m >= self._settings.min_volume_confirmation
            and snapshot.trade_rate_ratio >= self._settings.min_trade_confirmation
            and pressure >= self._settings.min_pressure_confirmation
        )
        if snapshot.direction is Direction.LONG:
            touched = [
                zone
                for zone in levels.resistance_zones
                if current.high >= zone.low and current.close < zone.low
            ]
            if touched:
                return BreakoutState.FAILED, min(
                    touched, key=lambda zone: abs(zone.midpoint - current.close)
                )
            crossed = [
                zone for zone in levels.resistance_zones if zone.high < current.close
            ]
            if not crossed:
                return BreakoutState.NONE, None
            level = max(crossed, key=lambda zone: zone.high)
            recent_below = any(
                item.close <= level.high * (1.0 + tolerance) for item in bars[-5:-1]
            )
            if recent_below or previous_close <= level.high * (1.0 + tolerance):
                state = BreakoutState.CONFIRMED if flow_confirmed else BreakoutState.IN_PROGRESS
                return state, level
        else:
            touched = [
                zone
                for zone in levels.support_zones
                if current.low <= zone.high and current.close > zone.high
            ]
            if touched:
                return BreakoutState.FAILED, min(
                    touched, key=lambda zone: abs(zone.midpoint - current.close)
                )
            crossed = [zone for zone in levels.support_zones if zone.low > current.close]
            if not crossed:
                return BreakoutState.NONE, None
            level = min(crossed, key=lambda zone: zone.low)
            recent_above = any(
                item.close >= level.low * (1.0 - tolerance) for item in bars[-5:-1]
            )
            if recent_above or previous_close >= level.low * (1.0 - tolerance):
                state = BreakoutState.CONFIRMED if flow_confirmed else BreakoutState.IN_PROGRESS
                return state, level
        return BreakoutState.NONE, None

    def _retest(
        self,
        direction: Direction,
        bars: Sequence[Kline],
        breakout_state: BreakoutState,
        level: PriceZone | None,
    ) -> RetestState:
        if level is None or breakout_state in {BreakoutState.NONE, BreakoutState.FAILED}:
            return (
                RetestState.FAILED
                if breakout_state is BreakoutState.FAILED
                else RetestState.NOT_APPLICABLE
            )
        tolerance = self._settings.retest_tolerance_pct / 100.0
        recent = bars[-3:]
        current = recent[-1]
        if direction is Direction.LONG:
            if bars[-2].close <= level.high * (1.0 + tolerance) and current.close > level.high:
                return RetestState.NOT_YET
            if current.close < level.low * (1.0 - tolerance):
                return RetestState.FAILED
            touched = any(
                item.low <= level.high * (1.0 + tolerance) for item in recent[-2:]
            )
            if touched and current.close >= level.high:
                return RetestState.HELD
            if touched:
                return RetestState.STARTED
        else:
            if bars[-2].close >= level.low * (1.0 - tolerance) and current.close < level.low:
                return RetestState.NOT_YET
            if current.close > level.high * (1.0 + tolerance):
                return RetestState.FAILED
            touched = any(
                item.high >= level.low * (1.0 - tolerance) for item in recent[-2:]
            )
            if touched and current.close <= level.low:
                return RetestState.HELD
            if touched:
                return RetestState.STARTED
        return RetestState.NOT_YET

    @staticmethod
    def _alignment(
        direction: Direction,
        one: TimeframeStructure,
        five: TimeframeStructure,
        fifteen: TimeframeStructure,
    ) -> float:
        desired = 1.0 if direction is Direction.LONG else -1.0
        values = (
            (_structure_value(one.structure), 0.25),
            (_structure_value(five.structure), 0.50),
            (_structure_value(fifteen.structure), 0.25),
        )
        return round(sum(value * weight * desired for value, weight in values) * 100.0, 2)

    def _late_score(
        self,
        snapshot: FeatureSnapshot,
        atr_pct: float,
        vwap_distance_pct: float,
        breakout_level: PriceZone | None,
        structure: TimeframeStructure,
    ) -> float:
        atr_multiple = abs(snapshot.return_5m) / max(atr_pct, 0.10)
        move_component = min(
            atr_multiple / (self._settings.late_atr_multiplier * 2.0),
            1.0,
        ) * 45.0
        parabolic_component = min(
            max(atr_multiple - self._settings.late_atr_multiplier, 0.0)
            / self._settings.late_atr_multiplier,
            1.0,
        ) * 15.0
        vwap_component = min(
            vwap_distance_pct / self._settings.max_distance_from_vwap_pct, 1.0
        ) * 20.0
        breakout_distance = (
            abs(snapshot.last_price / breakout_level.midpoint - 1.0) * 100.0
            if breakout_level is not None
            else 0.0
        )
        normalizer = max(atr_pct * self._settings.late_atr_multiplier, 0.10)
        breakout_component = min(breakout_distance / normalizer, 1.0) * 10.0
        pivot = (
            structure.last_swing_low
            if snapshot.direction is Direction.LONG
            else structure.last_swing_high
        )
        pivot_distance = (
            abs(snapshot.last_price / pivot - 1.0) * 100.0 if pivot else 0.0
        )
        pivot_component = min(pivot_distance / normalizer, 1.0) * 10.0
        return round(
            min(
                move_component
                + parabolic_component
                + vwap_component
                + breakout_component
                + pivot_component,
                100.0,
            ),
            2,
        )

    def _exhaustion_score(
        self,
        snapshot: FeatureSnapshot,
        bars: Sequence[Kline],
        atr_pct: float,
        vwap_distance_pct: float,
    ) -> float:
        wick = opposing_wick_ratio(bars[-1], snapshot.direction)
        wick_points = min(wick / self._settings.max_opposing_wick_ratio, 1.0) * 25.0
        recent = bars[-6:]
        previous_peak_volume = max((item.quote_volume for item in recent[:-1]), default=0.0)
        volume_drop = (
            previous_peak_volume > 0 and recent[-1].quote_volume < previous_peak_volume * 0.5
        )
        pressure = _directional_pressure(snapshot)
        pressure_points = (
            min((self._settings.min_pressure_confirmation - pressure) / 0.20, 1.0) * 20.0
            if pressure < self._settings.min_pressure_confirmation
            else 0.0
        )
        vwap_points = min(
            vwap_distance_pct / self._settings.max_distance_from_vwap_pct, 1.0
        ) * 20.0
        atr_move = abs(snapshot.return_5m) / max(atr_pct, 0.10)
        atr_points = min(atr_move / self._settings.late_atr_multiplier, 1.0) * 20.0
        volume_points = 15.0 if volume_drop else 0.0
        return round(
            min(wick_points + pressure_points + vwap_points + atr_points + volume_points, 100.0),
            2,
        )

    def _risk_model(
        self,
        snapshot: FeatureSnapshot,
        atr: float,
        support: PriceZone | None,
        resistance: PriceZone | None,
        breakout_level: PriceZone | None,
    ) -> tuple[float, float, float, float, float, float | None]:
        entry = snapshot.last_price
        fallback_atr = max(atr, entry * 0.005)
        buffer = fallback_atr * self._settings.volatility_buffer_atr
        if snapshot.direction is Direction.LONG:
            logical_support = breakout_level or support
            invalidation = (
                logical_support.low - buffer
                if logical_support is not None
                else entry - fallback_atr - buffer
            )
            target = (
                resistance.low
                if resistance is not None and resistance.low > entry
                else entry + fallback_atr * self._settings.target_atr_multiplier
            )
            risk = max(entry - invalidation, 0.0)
            reward = max(target - entry, 0.0)
        else:
            logical_resistance = breakout_level or resistance
            invalidation = (
                logical_resistance.high + buffer
                if logical_resistance is not None
                else entry + fallback_atr + buffer
            )
            target = (
                support.high
                if support is not None and support.high < entry
                else entry - fallback_atr * self._settings.target_atr_multiplier
            )
            risk = max(invalidation - entry, 0.0)
            reward = max(entry - target, 0.0)
        risk_pct = risk / max(entry, EPSILON) * 100.0
        reward_pct = reward / max(entry, EPSILON) * 100.0
        rr = reward / risk if risk > EPSILON else None
        return (
            entry,
            invalidation,
            target,
            round(risk_pct, 4),
            round(reward_pct, 4),
            round(rr, 3) if rr is not None else None,
        )

    def _entry_quality(
        self,
        *,
        snapshot: FeatureSnapshot,
        alignment: float,
        breakout_state: BreakoutState,
        retest_state: RetestState,
        room_pct: float | None,
        rr: float | None,
        late_score: float,
        exhaustion_score: float,
        vwap_distance_pct: float,
        opposing_wick: float,
    ) -> tuple[float, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        weights = self._settings.weights
        quality = weights.base_quality
        positive: list[str] = []
        negative: list[str] = []
        missing: list[str] = []
        if alignment >= 25:
            quality += weights.structure_aligned * min(alignment / 100.0, 1.0)
            positive.append("STRUCTURE_ALIGNED")
        elif alignment <= -25:
            quality -= weights.structure_conflict_penalty * min(abs(alignment) / 100.0, 1.0)
            negative.append("STRUCTURE_CONFLICT")
        if breakout_state is BreakoutState.CONFIRMED:
            quality += weights.breakout_confirmed
            positive.append("BREAKOUT_CONFIRMED")
        elif breakout_state is BreakoutState.FAILED:
            quality -= weights.failed_breakout_penalty
            negative.append("FAILED_BREAKOUT")
        if retest_state is RetestState.HELD:
            quality += weights.retest_held
            positive.append("RETEST_HELD")
        elif retest_state is RetestState.FAILED:
            negative.append("RETEST_FAILED")
        if snapshot.volume_ratio_1m >= self._settings.min_volume_confirmation:
            quality += weights.volume_confirmation
            positive.append("VOLUME_CONFIRMED")
        if snapshot.trade_rate_ratio >= self._settings.min_trade_confirmation:
            quality += weights.trades_confirmation
            positive.append("TRADES_CONFIRMED")
        if _directional_pressure(snapshot) >= self._settings.min_pressure_confirmation:
            quality += weights.pressure_confirmation
            positive.append("PRESSURE_CONFIRMED")
        directional_oi = _directional_sign(snapshot.direction) * snapshot.oi_delta_pct
        if snapshot.oi_data_ready or snapshot.deep_data_ready:
            if directional_oi >= self._settings.min_oi_delta_pct:
                quality += weights.oi_confirmation
                positive.append("OI_CONFIRMED")
            elif directional_oi <= -self._settings.min_oi_delta_pct:
                negative.append("OI_DIVERGENCE")
        else:
            missing.append("OI")
        if snapshot.spread_pct <= self._settings.healthy_spread_pct:
            quality += weights.spread_healthy
            positive.append("SPREAD_HEALTHY")
        directional_depth = _directional_sign(snapshot.direction) * snapshot.depth_imbalance
        if snapshot.depth_data_ready or snapshot.deep_data_ready:
            if directional_depth >= self._settings.min_depth_imbalance:
                quality += weights.depth_supportive
                positive.append("DEPTH_SUPPORTIVE")
            elif directional_depth <= -self._settings.min_depth_imbalance:
                negative.append("THIN_DIRECTIONAL_DEPTH")
        else:
            missing.append("DEPTH")
        if room_pct is not None and room_pct >= self._settings.min_room_pct:
            quality += weights.room_available
            positive.append("ROOM_AVAILABLE")
        elif room_pct is not None:
            negative.append("LEVEL_TOO_CLOSE")
        else:
            missing.append("TARGET_LEVEL")
        if rr is not None and rr >= self._settings.min_rr:
            quality += weights.rr_acceptable
            positive.append("RR_ACCEPTABLE")
        elif rr is not None:
            negative.append("BAD_RR")
        if late_score >= self._settings.late_score_threshold:
            quality -= weights.late_penalty
            negative.append("LATE_ENTRY")
        if exhaustion_score >= self._settings.exhaustion_threshold:
            quality -= weights.exhaustion_penalty
            negative.append("MOMENTUM_EXHAUSTED")
        if vwap_distance_pct > self._settings.max_distance_from_vwap_pct:
            quality -= weights.vwap_distance_penalty
            negative.append("TOO_FAR_FROM_VWAP")
        if opposing_wick > self._settings.max_opposing_wick_ratio:
            quality -= weights.opposing_wick_penalty
            negative.append("LARGE_OPPOSING_WICK")
        return (
            round(min(max(quality, 0.0), 100.0), 2),
            tuple(positive),
            tuple(negative),
            tuple(missing),
        )

def _directional_pressure(snapshot: FeatureSnapshot) -> float:
    pressure = (
        snapshot.agg_buy_pressure
        if snapshot.trade_data_ready or snapshot.deep_data_ready
        else snapshot.buy_pressure
    )
    return pressure if snapshot.direction is Direction.LONG else 1.0 - pressure


def _directional_sign(direction: Direction) -> float:
    return 1.0 if direction is Direction.LONG else -1.0


def _structure_value(structure: MarketStructure) -> float:
    if structure is MarketStructure.BULLISH:
        return 1.0
    if structure is MarketStructure.BEARISH:
        return -1.0
    return 0.0


def _distance_below(price: float, zone: PriceZone | None) -> float | None:
    if zone is None or zone.high >= price:
        return None
    return round((price / zone.high - 1.0) * 100.0, 4)


def _distance_above(price: float, zone: PriceZone | None) -> float | None:
    if zone is None or zone.low <= price:
        return None
    return round((zone.low / price - 1.0) * 100.0, 4)


def select_entry_decision(
    settings: StageCSettings,
    direction: Direction,
    quality: float,
    breakout_state: BreakoutState,
    retest_state: RetestState,
    room_pct: float | None,
    rr: float | None,
    alignment: float,
    late_score: float,
    exhaustion_score: float,
) -> EntryDecision:
    """Pure final decision with explicit precedence. / Чистое итоговое решение."""

    if breakout_state is BreakoutState.FAILED or retest_state is RetestState.FAILED:
        return EntryDecision.INVALIDATED
    if late_score >= settings.late_score_threshold:
        return EntryDecision.SKIP_LATE
    if exhaustion_score >= settings.exhaustion_threshold:
        return EntryDecision.SKIP_EXHAUSTION
    if alignment <= -50:
        return EntryDecision.SKIP_STRUCTURE_CONFLICT
    if room_pct is not None and room_pct < settings.min_room_pct:
        return (
            EntryDecision.SKIP_RESISTANCE_TOO_CLOSE
            if direction is Direction.LONG
            else EntryDecision.SKIP_SUPPORT_TOO_CLOSE
        )
    if rr is not None and rr < settings.min_rr:
        return EntryDecision.SKIP_BAD_RR
    if breakout_state in {BreakoutState.CONFIRMED, BreakoutState.IN_PROGRESS}:
        if retest_state is RetestState.HELD:
            return EntryDecision.ENTER_CANDIDATE
        return EntryDecision.WAIT_RETEST
    if quality >= settings.min_entry_quality:
        return EntryDecision.WATCH
    return EntryDecision.WATCH
