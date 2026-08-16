"""Debounced signal finite-state machine. / Машина состояний сигнала с debounce."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import structlog

from pumplens.analytics.buffers import MarketState
from pumplens.analytics.levels import SignalLevels, calculate_levels
from pumplens.config import EarlySettings, LateSettings, ScannerSettings, StageCSettings
from pumplens.domain.enums import DataQuality, Direction, EntryDecision, SignalState
from pumplens.domain.models import Candidate, FeatureSnapshot

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class SignalLifecycle:
    symbol: str
    direction: Direction
    state: SignalState = SignalState.NORMAL
    start_price: float | None = None
    trigger_price: float | None = None
    levels: SignalLevels | None = None
    state_changed_at: datetime | None = None
    confirmed_score_since: datetime | None = None
    weak_since: datetime | None = None
    confirmed_at: datetime | None = None
    cooldown_until: datetime | None = None
    early_weak_since: datetime | None = None
    early_liquidity_tier: str | None = None
    short_rearm: bool = False


@dataclass(frozen=True, slots=True)
class SignalTransition:
    symbol: str
    direction: Direction
    from_state: SignalState
    to_state: SignalState
    reason_code: str
    reason_text: str
    timestamp: datetime
    snapshot: FeatureSnapshot
    levels: SignalLevels | None
    liquidity_tier: str | None = None


class SignalFSM:
    def __init__(
        self,
        state: MarketState,
        scanner_settings: ScannerSettings,
        late_settings: LateSettings,
        early_settings: EarlySettings | None = None,
        stage_c_settings: StageCSettings | None = None,
    ) -> None:
        self._market_state = state
        self._settings = scanner_settings
        self._late = late_settings
        self._early = early_settings or EarlySettings()
        self._stage_c = stage_c_settings or StageCSettings()
        self._lifecycles: dict[tuple[str, Direction], SignalLifecycle] = {}

    def lifecycle(self, symbol: str, direction: Direction) -> SignalLifecycle:
        key = (symbol, direction)
        if key not in self._lifecycles:
            self._lifecycles[key] = SignalLifecycle(symbol=symbol, direction=direction)
        return self._lifecycles[key]

    def advance(
        self,
        candidate: Candidate,
        now: datetime | None = None,
    ) -> SignalTransition | None:
        now = now or datetime.now(UTC)
        snapshot = candidate.snapshot
        direction_flip = self._direction_flip(snapshot, now)
        if direction_flip is not None:
            return direction_flip
        lifecycle = self.lifecycle(snapshot.symbol, snapshot.direction)

        if snapshot.data_quality is not DataQuality.FRESH:
            lifecycle.confirmed_score_since = None
            if lifecycle.state in {
                SignalState.CANDIDATE,
                SignalState.EARLY,
                SignalState.WATCH,
                SignalState.CONFIRMED,
            }:
                if lifecycle.state is SignalState.EARLY:
                    return self._close_early(
                        lifecycle,
                        SignalState.INVALIDATED,
                        "DATA_STALE",
                        now,
                        snapshot,
                    )
                return self._transition(
                    lifecycle,
                    SignalState.INVALIDATED,
                    "DATA_STALE",
                    now,
                    snapshot,
                )
            return None

        analysis = snapshot.entry_analysis
        if (
            analysis is not None
            and analysis.final_decision is EntryDecision.INVALIDATED
            and lifecycle.state
            in {
                SignalState.CANDIDATE,
                SignalState.EARLY,
                SignalState.WATCH,
                SignalState.CONFIRMED,
            }
        ):
            return self._transition(
                lifecycle,
                SignalState.INVALIDATED,
                "STAGE_C_INVALIDATED",
                now,
                snapshot,
            )

        # TOO_LATE wins over confirmation in the same measurement.
        # TOO_LATE имеет приоритет над подтверждением в одном измерении.
        if candidate.hard_reject_reason == "too_late" and lifecycle.state not in {
            SignalState.NORMAL,
            SignalState.COOLDOWN,
            SignalState.TOO_LATE,
        }:
            if lifecycle.state is SignalState.EARLY:
                return self._close_early(
                    lifecycle,
                    SignalState.TOO_LATE,
                    "TOO_LATE",
                    now,
                    snapshot,
                )
            return self._transition(lifecycle, SignalState.TOO_LATE, "TOO_LATE", now, snapshot)

        if lifecycle.state is SignalState.NORMAL:
            if (
                candidate.hard_reject_reason == "too_late"
                and snapshot.entry_analysis is not None
            ):
                return self._transition(
                    lifecycle,
                    SignalState.TOO_LATE,
                    "STAGE_C_LATE",
                    now,
                    snapshot,
                )
            if candidate.selected and candidate.hard_reject_reason is None:
                lifecycle.start_price = snapshot.last_price
                return self._transition(
                    lifecycle,
                    SignalState.CANDIDATE,
                    "STAGE_A_DEBOUNCED",
                    now,
                    snapshot,
                )
            return None

        if lifecycle.state is SignalState.CANDIDATE:
            if candidate.early_selected and candidate.hard_reject_reason is None:
                early_snapshot = candidate.early_snapshot or snapshot
                lifecycle.early_liquidity_tier = candidate.early_liquidity_tier
                return self._transition(
                    lifecycle,
                    SignalState.EARLY,
                    "EARLY_FLASH_DEBOUNCED",
                    now,
                    early_snapshot,
                )
            if (
                analysis is not None
                and analysis.final_decision
                in {
                    EntryDecision.SKIP_BAD_RR,
                    EntryDecision.SKIP_RESISTANCE_TOO_CLOSE,
                    EntryDecision.SKIP_SUPPORT_TOO_CLOSE,
                    EntryDecision.SKIP_EXHAUSTION,
                    EntryDecision.SKIP_STRUCTURE_CONFLICT,
                }
            ):
                return self._transition(
                    lifecycle,
                    SignalState.INVALIDATED,
                    f"STAGE_C_{analysis.final_decision.value}",
                    now,
                    snapshot,
                )
            if (
                self._watch_confirmations(candidate)
                and snapshot.score >= self._settings.watch_score
                and self._market_confirmation_ready(snapshot)
                and self._stage_c_allows_watch(snapshot)
            ):
                lifecycle.trigger_price = snapshot.last_price
                lifecycle.levels = self._calculate_levels(lifecycle, snapshot)
                return self._transition(
                    lifecycle,
                    SignalState.WATCH,
                    "WATCH_THRESHOLD",
                    now,
                    snapshot,
                )
            return None

        if lifecycle.state is SignalState.EARLY:
            if self._early_limit_crossed(snapshot):
                return self._close_early(
                    lifecycle,
                    SignalState.TOO_LATE,
                    "EARLY_LIMIT_CROSSED",
                    now,
                    snapshot,
                )
            if (
                self._watch_confirmations(candidate)
                and snapshot.score >= self._settings.watch_score
                and self._market_confirmation_ready(snapshot)
                and self._stage_c_allows_watch(snapshot)
            ):
                lifecycle.trigger_price = snapshot.last_price
                lifecycle.levels = self._calculate_levels(lifecycle, snapshot)
                lifecycle.short_rearm = False
                return self._transition(
                    lifecycle,
                    SignalState.WATCH,
                    "WATCH_THRESHOLD",
                    now,
                    snapshot,
                )
            invalidation_reason = self._early_invalidation_reason(
                lifecycle,
                candidate,
                now,
            )
            if invalidation_reason is not None:
                return self._close_early(
                    lifecycle,
                    SignalState.INVALIDATED,
                    invalidation_reason,
                    now,
                    snapshot,
                )
            return None

        if lifecycle.state is SignalState.WATCH:
            invalidation_reason = self._invalidation_reason(lifecycle, snapshot, now)
            if invalidation_reason is not None:
                return self._transition(
                    lifecycle,
                    SignalState.INVALIDATED,
                    invalidation_reason,
                    now,
                    snapshot,
                )

            if (
                self._market_confirmation_ready(snapshot)
                and snapshot.score >= self._settings.confirmed_score
                and self._stage_c_allows_watch(snapshot)
            ):
                lifecycle.confirmed_score_since = lifecycle.confirmed_score_since or now
                if now - lifecycle.confirmed_score_since >= timedelta(seconds=5):
                    lifecycle.confirmed_at = now
                    return self._transition(
                        lifecycle,
                        SignalState.CONFIRMED,
                        "SCORE_HELD_5S",
                        now,
                        snapshot,
                    )
            else:
                lifecycle.confirmed_score_since = None
            return None

        if lifecycle.state is SignalState.CONFIRMED:
            if self._confirmed_complete(lifecycle, snapshot, now):
                lifecycle.cooldown_until = now + timedelta(minutes=self._settings.cooldown_minutes)
                return self._transition(
                    lifecycle,
                    SignalState.COOLDOWN,
                    "CONFIRMED_COMPLETE",
                    now,
                    snapshot,
                )
            return None

        if lifecycle.state in {SignalState.INVALIDATED, SignalState.TOO_LATE}:
            if lifecycle.cooldown_until is None:
                lifecycle.cooldown_until = now + timedelta(minutes=self._settings.cooldown_minutes)
            return self._transition(
                lifecycle,
                SignalState.COOLDOWN,
                "TERMINAL_COOLDOWN",
                now,
                snapshot,
            )

        if (
            lifecycle.state is SignalState.COOLDOWN
            and lifecycle.cooldown_until is not None
            and now >= lifecycle.cooldown_until
        ):
            self._reset(lifecycle)
            return self._transition(
                lifecycle,
                SignalState.NORMAL,
                "COOLDOWN_EXPIRED",
                now,
                snapshot,
            )
        return None

    def _direction_flip(
        self,
        snapshot: FeatureSnapshot,
        now: datetime,
    ) -> SignalTransition | None:
        """Close the opposite active signal. / Закрывает активный обратный сигнал."""

        opposite = Direction.SHORT if snapshot.direction is Direction.LONG else Direction.LONG
        lifecycle = self._lifecycles.get((snapshot.symbol, opposite))
        if lifecycle is None or lifecycle.state not in {
            SignalState.CANDIDATE,
            SignalState.EARLY,
            SignalState.WATCH,
            SignalState.CONFIRMED,
        }:
            return None
        if lifecycle.state is SignalState.EARLY:
            lifecycle.cooldown_until = now + timedelta(seconds=self._early.rearm_seconds)
            lifecycle.short_rearm = True
            target = SignalState.INVALIDATED
            reason = "EARLY_DIRECTION_FLIP"
        else:
            lifecycle.cooldown_until = now + timedelta(minutes=self._settings.cooldown_minutes)
            target = SignalState.COOLDOWN
            reason = "DIRECTION_FLIP"
        # Persist the old direction consistently even though the new snapshot flipped.
        # Сохраняем старое направление, хотя новый snapshot уже развернулся.
        opposite_snapshot = replace(snapshot, direction=opposite)
        return self._transition(
            lifecycle,
            target,
            reason,
            now,
            opposite_snapshot,
        )

    @staticmethod
    def _watch_confirmations(candidate: Candidate) -> bool:
        confirmations = set(candidate.confirmations)
        return (
            "price" in confirmations
            and "volume" in confirmations
            and bool({"pressure", "trade_rate"} & confirmations)
        )

    def _stage_c_allows_watch(self, snapshot: FeatureSnapshot) -> bool:
        if not self._stage_c.enabled or not self._stage_c.require_for_watch:
            return True
        analysis = snapshot.entry_analysis
        if analysis is None or analysis.entry_quality < self._stage_c.min_entry_quality:
            return False
        return analysis.final_decision in {
            EntryDecision.ENTER_CANDIDATE,
            EntryDecision.WATCH,
        }

    def _market_confirmation_ready(self, snapshot: FeatureSnapshot) -> bool:
        if self._stage_c.enabled and self._stage_c.require_for_watch:
            return snapshot.trade_data_ready or snapshot.deep_data_ready
        return snapshot.deep_data_ready

    def _calculate_levels(
        self,
        lifecycle: SignalLifecycle,
        snapshot: FeatureSnapshot,
    ) -> SignalLevels:
        buffer = self._market_state.get(snapshot.symbol)
        recent = list(buffer.closed_klines) if buffer is not None else []
        levels = calculate_levels(
            snapshot.direction,
            lifecycle.start_price or snapshot.last_price,
            snapshot.last_price,
            recent,
            self._late,
        )
        analysis = snapshot.entry_analysis
        if analysis is None:
            return levels
        return replace(
            levels,
            invalidation=analysis.invalidation_price,
            risk_distance_pct=analysis.risk_pct,
        )

    def _early_limit_crossed(self, snapshot: FeatureSnapshot) -> bool:
        return (
            abs(snapshot.return_1m) > self._early.max_return_1m_pct
            or abs(snapshot.return_5m) >= self._early.max_return_5m_pct
        )

    def _early_invalidation_reason(
        self,
        lifecycle: SignalLifecycle,
        candidate: Candidate,
        now: datetime,
    ) -> str | None:
        snapshot = candidate.snapshot
        pressure = (
            snapshot.buy_pressure
            if snapshot.direction is Direction.LONG
            else 1.0 - snapshot.buy_pressure
        )
        if pressure <= self._early.pressure_reversal:
            return "EARLY_PRESSURE_REVERSED"
        if snapshot.spread_pct > self._early.max_spread_pct:
            return "EARLY_SPREAD_WIDE"
        start_price = lifecycle.start_price or snapshot.last_price
        signed_move = (
            (snapshot.last_price / start_price - 1.0)
            * 100.0
            * (1.0 if snapshot.direction is Direction.LONG else -1.0)
        )
        if signed_move <= -self._early.against_move_pct:
            return "EARLY_PRICE_REVERSED"
        if candidate.early_selected:
            lifecycle.early_weak_since = None
            return None
        lifecycle.early_weak_since = lifecycle.early_weak_since or now
        if now - lifecycle.early_weak_since >= timedelta(
            seconds=self._early.invalidate_after_seconds
        ):
            return "EARLY_WEAK"
        return None

    def _close_early(
        self,
        lifecycle: SignalLifecycle,
        target: SignalState,
        reason: str,
        now: datetime,
        snapshot: FeatureSnapshot,
    ) -> SignalTransition:
        lifecycle.cooldown_until = now + timedelta(seconds=self._early.rearm_seconds)
        lifecycle.short_rearm = True
        return self._transition(lifecycle, target, reason, now, snapshot)

    @staticmethod
    def _invalidation_reason(
        lifecycle: SignalLifecycle,
        snapshot: FeatureSnapshot,
        now: datetime,
    ) -> str | None:
        levels = lifecycle.levels
        if levels is not None:
            crossed = (
                snapshot.direction is Direction.LONG
                and snapshot.last_price <= levels.invalidation
            ) or (
                snapshot.direction is Direction.SHORT
                and snapshot.last_price >= levels.invalidation
            )
            if crossed:
                return "PRICE_INVALIDATION"

        directional_pressure = (
            snapshot.agg_buy_pressure
            if snapshot.direction is Direction.LONG
            else 1.0 - snapshot.agg_buy_pressure
        )
        weak = snapshot.score < 50 or (snapshot.deep_data_ready and directional_pressure < 0.45)
        if weak:
            lifecycle.weak_since = lifecycle.weak_since or now
            if now - lifecycle.weak_since >= timedelta(seconds=15):
                return "WEAK_15S"
        else:
            lifecycle.weak_since = None
        return None

    @staticmethod
    def _confirmed_complete(
        lifecycle: SignalLifecycle,
        snapshot: FeatureSnapshot,
        now: datetime,
    ) -> bool:
        if lifecycle.confirmed_at and now - lifecycle.confirmed_at >= timedelta(minutes=15):
            return True
        trigger = lifecycle.trigger_price or snapshot.last_price
        signed_move = (
            (snapshot.last_price / trigger - 1.0) * 100.0
            * (1.0 if snapshot.direction is Direction.LONG else -1.0)
        )
        if signed_move >= 5.0 or signed_move <= -3.0:
            return True
        levels = lifecycle.levels
        if levels is None:
            return False
        return (
            snapshot.direction is Direction.LONG
            and snapshot.last_price <= levels.invalidation
        ) or (
            snapshot.direction is Direction.SHORT
            and snapshot.last_price >= levels.invalidation
        )

    @staticmethod
    def _reset(lifecycle: SignalLifecycle) -> None:
        lifecycle.start_price = None
        lifecycle.trigger_price = None
        lifecycle.levels = None
        lifecycle.confirmed_score_since = None
        lifecycle.weak_since = None
        lifecycle.confirmed_at = None
        lifecycle.early_weak_since = None
        lifecycle.early_liquidity_tier = None
        lifecycle.short_rearm = False
        lifecycle.cooldown_until = None

    @staticmethod
    def _transition(
        lifecycle: SignalLifecycle,
        target: SignalState,
        reason: str,
        now: datetime,
        snapshot: FeatureSnapshot,
    ) -> SignalTransition:
        previous = lifecycle.state
        lifecycle.state = target
        lifecycle.state_changed_at = now
        transition = SignalTransition(
            symbol=lifecycle.symbol,
            direction=lifecycle.direction,
            from_state=previous,
            to_state=target,
            reason_code=reason,
            reason_text=reason.replace("_", " ").lower(),
            timestamp=now,
            snapshot=snapshot,
            levels=lifecycle.levels,
            liquidity_tier=lifecycle.early_liquidity_tier,
        )
        log.info(
            "signal_transition",
            symbol=lifecycle.symbol,
            direction=lifecycle.direction.value,
            from_state=previous.value,
            to_state=target.value,
            reason=reason,
            score=snapshot.score,
            entry_quality=(
                snapshot.entry_analysis.entry_quality
                if snapshot.entry_analysis is not None
                else None
            ),
            decision=(
                snapshot.entry_analysis.final_decision.value
                if snapshot.entry_analysis is not None
                else None
            ),
        )
        return transition
