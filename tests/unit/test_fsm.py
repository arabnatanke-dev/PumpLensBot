"""Signal FSM scenario tests. / Сценарные тесты машины состояний."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from pumplens.analytics.buffers import MarketState
from pumplens.analytics.fsm import SignalFSM
from pumplens.config import LateSettings, ScannerSettings
from pumplens.domain.enums import DataQuality, Direction, SignalState
from pumplens.domain.models import Candidate, FeatureSnapshot, Kline


def snapshot(score: float = 75) -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol="TESTUSDT",
        timestamp=datetime.now(UTC),
        direction=Direction.LONG,
        last_price=102,
        return_1m=1.2,
        return_3m=1.8,
        return_5m=2,
        return_15m=3,
        acceleration_1m=0.5,
        volume_ratio_1m=4,
        volume_robust_z=5,
        buy_pressure=0.66,
        trade_rate_ratio=3,
        spread_pct=0.05,
        relative_strength_1m=1,
        range_pct_1m=2,
        candle_structure=0.9,
        quote_volume_24h=10_000_000,
        agg_buy_pressure=0.67,
        agg_trade_rate_ratio=3,
        deep_data_ready=True,
        score=score,
        data_quality=DataQuality.FRESH,
    )


def candidate(score: float = 75, hard_reject: str | None = None) -> Candidate:
    return Candidate(
        snapshot=snapshot(score),
        selected=True,
        hard_reject_reason=hard_reject,
        confirmations=("price", "volume", "pressure", "trade_rate"),
    )


def seeded_state() -> MarketState:
    state = MarketState(["TESTUSDT"])
    state.seed(
        "TESTUSDT",
        [
            Kline(
                "TESTUSDT",
                index * 60_000,
                index * 60_000 + 59_999,
                100,
                103,
                99,
                102,
                1,
                1,
                1,
                0.5,
            )
            for index in range(3)
        ],
    )
    return state


def test_fsm_does_not_skip_candidate_and_watch() -> None:
    fsm = SignalFSM(seeded_state(), ScannerSettings(), LateSettings())
    now = datetime.now(UTC)
    first = fsm.advance(candidate(), now)
    second = fsm.advance(candidate(), now + timedelta(seconds=1))
    assert first is not None and first.to_state is SignalState.CANDIDATE
    assert second is not None and second.to_state is SignalState.WATCH


def test_confirmed_score_must_hold_for_five_seconds() -> None:
    fsm = SignalFSM(seeded_state(), ScannerSettings(), LateSettings())
    now = datetime.now(UTC)
    fsm.advance(candidate(), now)
    fsm.advance(candidate(), now + timedelta(seconds=1))
    assert fsm.advance(candidate(90), now + timedelta(seconds=2)) is None
    transition = fsm.advance(candidate(90), now + timedelta(seconds=7))
    assert transition is not None and transition.to_state is SignalState.CONFIRMED


def test_too_late_has_priority_over_confirmation() -> None:
    fsm = SignalFSM(seeded_state(), ScannerSettings(), LateSettings())
    now = datetime.now(UTC)
    fsm.advance(candidate(), now)
    fsm.advance(candidate(), now + timedelta(seconds=1))
    too_late = replace(candidate(95, "too_late"), snapshot=replace(snapshot(95), return_5m=9))
    transition = fsm.advance(too_late, now + timedelta(seconds=10))
    assert transition is not None and transition.to_state is SignalState.TOO_LATE
