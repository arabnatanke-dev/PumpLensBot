"""Debounced signal finite-state machine. / Машина состояний сигнала с debounce."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from pumplens.analytics.buffers import MarketState
from pumplens.analytics.levels import SignalLevels, calculate_levels
from pumplens.config import LateSettings, ScannerSettings
from pumplens.domain.enums import Direction, SignalState
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


class SignalFSM:
    def __init__(
        self,
        state: MarketState,
        scanner_settings: ScannerSettings,
        late_settings: LateSettings,
    ) -> None:
        self._market_state = state
        self._settings = scanner_settings
        self._late = late_settings
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
        lifecycle = self.lifecycle(snapshot.symbol, snapshot.direction)

        # TOO_LATE wins over confirmation in the same measurement.
        # TOO_LATE имеет приоритет над подтверждением в одном измерении.
        if candidate.hard_reject_reason == "too_late" and lifecycle.state not in {
            SignalState.NORMAL,
            SignalState.COOLDOWN,
            SignalState.TOO_LATE,
        }:
            return self._transition(lifecycle, SignalState.TOO_LATE, "TOO_LATE", now, snapshot)

        if lifecycle.state is SignalState.NORMAL:
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
            if (
                self._watch_confirmations(candidate)
                and snapshot.score >= self._settings.watch_score
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

            if snapshot.score >= self._settings.confirmed_score:
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

    @staticmethod
    def _watch_confirmations(candidate: Candidate) -> bool:
        confirmations = set(candidate.confirmations)
        return (
            "price" in confirmations
            and "volume" in confirmations
            and bool({"pressure", "trade_rate"} & confirmations)
        )

    def _calculate_levels(
        self,
        lifecycle: SignalLifecycle,
        snapshot: FeatureSnapshot,
    ) -> SignalLevels:
        buffer = self._market_state.get(snapshot.symbol)
        recent = list(buffer.closed_klines) if buffer is not None else []
        return calculate_levels(
            snapshot.direction,
            lifecycle.start_price or snapshot.last_price,
            snapshot.last_price,
            recent,
            self._late,
        )

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
        )
        log.info(
            "signal_transition",
            symbol=lifecycle.symbol,
            direction=lifecycle.direction.value,
            from_state=previous.value,
            to_state=target.value,
            reason=reason,
            score=snapshot.score,
        )
        return transition
