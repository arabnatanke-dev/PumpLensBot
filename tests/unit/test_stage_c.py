"""Stage C market structure and entry validation. / Глубокая проверка входа."""

from __future__ import annotations

import math
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from pumplens.analytics.buffers import MarketState
from pumplens.analytics.fsm import SignalFSM, SignalTransition
from pumplens.analytics.market_structure import (
    analyze_timeframe,
    build_level_map,
    nearest_zones,
)
from pumplens.analytics.stage_c import DeepEntryValidator, select_entry_decision
from pumplens.config import LateSettings, ScannerSettings, StageCSettings
from pumplens.domain.enums import (
    BreakoutState,
    DataQuality,
    Direction,
    EntryDecision,
    MarketStructure,
    RetestState,
    SignalState,
)
from pumplens.domain.models import Candidate, FeatureSnapshot, Kline
from pumplens.storage.db import Database
from pumplens.storage.models import Base, SignalFeatureRecord, SignalRecord
from pumplens.storage.repositories import SignalRepository
from pumplens.telegram.notifications import format_signal, format_signal_explanation


def _bar(symbol: str, index: int, open_: float, close: float, *, closed: bool = True) -> Kline:
    return Kline(
        symbol=symbol,
        open_time_ms=index * 60_000,
        close_time_ms=index * 60_000 + 59_999,
        open=open_,
        high=max(open_, close) + 0.15,
        low=min(open_, close) - 0.15,
        close=close,
        base_volume=10,
        quote_volume=10_000,
        trade_count=100,
        taker_buy_quote_volume=6_500,
        closed=closed,
    )


def _wave_bars(symbol: str, direction: Direction, count: int = 120) -> list[Kline]:
    sign = 1.0 if direction is Direction.LONG else -1.0
    result: list[Kline] = []
    previous = 100.0
    for index in range(count):
        close = 100.0 + sign * index * 0.06 + sign * math.sin(index * math.pi / 4) * 0.7
        result.append(_bar(symbol, index, previous, close))
        previous = close
    return result


def _snapshot(
    symbol: str,
    direction: Direction,
    price: float,
    *,
    return_5m: float | None = None,
    deep: bool = True,
) -> FeatureSnapshot:
    sign = 1.0 if direction is Direction.LONG else -1.0
    return FeatureSnapshot(
        symbol=symbol,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        direction=direction,
        last_price=price,
        return_1m=sign,
        return_3m=sign * 1.5,
        return_5m=return_5m if return_5m is not None else sign * 2.0,
        return_15m=sign * 3.0,
        acceleration_1m=sign * 0.5,
        volume_ratio_1m=5.0,
        volume_robust_z=5.0,
        buy_pressure=0.72 if direction is Direction.LONG else 0.28,
        trade_rate_ratio=4.0,
        spread_pct=0.05,
        relative_strength_1m=sign,
        range_pct_1m=1.0,
        candle_structure=0.9,
        quote_volume_24h=100_000_000,
        agg_buy_pressure=0.72 if direction is Direction.LONG else 0.28,
        agg_trade_rate_ratio=4.0,
        depth_imbalance=sign * 0.20 if deep else 0.0,
        bid_depth_usdt=10_000 if deep else 0.0,
        ask_depth_usdt=10_000 if deep else 0.0,
        oi_delta_pct=sign * 0.20 if deep else 0.0,
        trade_data_ready=True,
        depth_data_ready=deep,
        oi_data_ready=deep,
        deep_data_ready=deep,
        score=85.0,
        data_quality=DataQuality.FRESH,
    )


def _stage_c_case(
    direction: Direction,
    mode: str,
    *,
    return_5m: float | None = None,
    deep: bool = True,
) -> tuple[MarketState, Candidate]:
    symbol = f"{direction.value}{mode.upper()}USDT"
    closed = _wave_bars(symbol, direction)
    levels = build_level_map(closed, pivot_window=2, tolerance_pct=0.25)
    support, resistance = nearest_zones(levels, closed[-1].close)
    level = resistance if direction is Direction.LONG else support
    assert level is not None

    if mode == "retest":
        breakout_close = (
            level.high * 1.005
            if direction is Direction.LONG
            else level.low * 0.995
        )
        closed.append(_bar(symbol, len(closed), closed[-1].close, breakout_close))
        current_close = (
            level.high * 1.004
            if direction is Direction.LONG
            else level.low * 0.996
        )
        current = _bar(symbol, len(closed), breakout_close, current_close, closed=False)
        current = replace(
            current,
            low=level.high * 0.999 if direction is Direction.LONG else current.low,
            high=level.low * 1.001 if direction is Direction.SHORT else current.high,
        )
    elif mode == "fake":
        current_close = (
            level.low * 0.998
            if direction is Direction.LONG
            else level.high * 1.002
        )
        current = _bar(symbol, len(closed), closed[-1].close, current_close, closed=False)
        current = replace(
            current,
            high=level.high * 1.004 if direction is Direction.LONG else current.high,
            low=level.low * 0.996 if direction is Direction.SHORT else current.low,
        )
    else:
        current_close = (
            level.high * 1.005
            if direction is Direction.LONG
            else level.low * 0.995
        )
        current = _bar(symbol, len(closed), closed[-1].close, current_close, closed=False)

    state = MarketState([symbol])
    state.seed(symbol, closed)
    buffer = state.get(symbol)
    assert buffer is not None
    buffer.current_kline = current
    snapshot = _snapshot(
        symbol,
        direction,
        current_close,
        return_5m=return_5m,
        deep=deep,
    )
    return state, Candidate(
        snapshot=snapshot,
        selected=True,
        confirmations=("price", "volume", "pressure", "trade_rate"),
    )


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        (Direction.LONG, MarketStructure.BULLISH),
        (Direction.SHORT, MarketStructure.BEARISH),
    ],
)
def test_market_structure_detects_directional_swings(
    direction: Direction,
    expected: MarketStructure,
) -> None:
    sign = 1.0 if direction is Direction.LONG else -1.0
    closes = [100, 102, 101, 103, 102, 104, 103, 105, 104, 106, 105, 107]
    closes = [100 + sign * (value - 100) for value in closes]
    bars = [_bar("SWINGUSDT", index, close, close) for index, close in enumerate(closes)]
    result = analyze_timeframe(
        bars,
        timeframe="1m",
        pivot_window=1,
        compression_ratio=0.7,
        expansion_ratio=1.5,
    )
    assert result.structure is expected
    assert result.pattern == ("HH_HL" if direction is Direction.LONG else "LH_LL")


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_clean_breakout_waits_for_retest(direction: Direction) -> None:
    state, candidate = _stage_c_case(direction, "clean")
    settings = StageCSettings(
        late_score_threshold=95,
        exhaustion_threshold=95,
        min_room_pct=0.01,
        min_rr=0.1,
    )
    result = DeepEntryValidator(state, settings).validate([candidate])[0]
    analysis = result.snapshot.entry_analysis
    assert analysis is not None
    assert analysis.breakout_state is BreakoutState.CONFIRMED
    assert analysis.retest_state is RetestState.NOT_YET
    assert analysis.final_decision is EntryDecision.WAIT_RETEST


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_breakout_retest_hold_becomes_entry_candidate(direction: Direction) -> None:
    state, candidate = _stage_c_case(direction, "retest")
    settings = StageCSettings(
        late_score_threshold=95,
        exhaustion_threshold=95,
        min_room_pct=0.01,
        min_rr=0.1,
    )
    analysis = DeepEntryValidator(state, settings).validate([candidate])[0].snapshot.entry_analysis
    assert analysis is not None
    assert analysis.breakout_state is BreakoutState.CONFIRMED
    assert analysis.retest_state is RetestState.HELD
    assert analysis.final_decision is EntryDecision.ENTER_CANDIDATE


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_failed_breakout_is_invalidated(direction: Direction) -> None:
    state, candidate = _stage_c_case(direction, "fake")
    analysis = (
        DeepEntryValidator(state, StageCSettings())
        .validate([candidate])[0]
        .snapshot.entry_analysis
    )
    assert analysis is not None
    assert analysis.breakout_state is BreakoutState.FAILED
    assert analysis.final_decision is EntryDecision.INVALIDATED


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_large_atr_normalized_move_is_too_late(direction: Direction) -> None:
    sign = 1.0 if direction is Direction.LONG else -1.0
    state, candidate = _stage_c_case(direction, "late", return_5m=sign * 20.0)
    result = DeepEntryValidator(state, StageCSettings()).validate([candidate])[0]
    analysis = result.snapshot.entry_analysis
    assert analysis is not None and analysis.late is True
    assert analysis.final_decision is EntryDecision.SKIP_LATE
    assert result.hard_reject_reason == "too_late"


@pytest.mark.parametrize(
    ("direction", "room", "rr", "alignment", "exhaustion", "expected"),
    [
        (Direction.LONG, 0.10, 2.0, 50.0, 10.0, EntryDecision.SKIP_RESISTANCE_TOO_CLOSE),
        (Direction.SHORT, 0.10, 2.0, 50.0, 10.0, EntryDecision.SKIP_SUPPORT_TOO_CLOSE),
        (Direction.LONG, 2.00, 0.8, 50.0, 10.0, EntryDecision.SKIP_BAD_RR),
        (Direction.SHORT, 2.00, 2.0, -75.0, 10.0, EntryDecision.SKIP_STRUCTURE_CONFLICT),
        (Direction.LONG, 2.00, 2.0, 50.0, 90.0, EntryDecision.SKIP_EXHAUSTION),
    ],
)
def test_entry_decision_reject_scenarios(
    direction: Direction,
    room: float,
    rr: float,
    alignment: float,
    exhaustion: float,
    expected: EntryDecision,
) -> None:
    decision = select_entry_decision(
        StageCSettings(),
        direction,
        quality=80,
        breakout_state=BreakoutState.NONE,
        retest_state=RetestState.NOT_APPLICABLE,
        room_pct=room,
        rr=rr,
        alignment=alignment,
        late_score=10,
        exhaustion_score=exhaustion,
    )
    assert decision is expected


def test_stage_c_limit_prevents_all_universe_analysis() -> None:
    candidates: list[Candidate] = []
    symbols = [f"LIMIT{index}USDT" for index in range(10)]
    state = MarketState(symbols)
    for index, symbol in enumerate(symbols):
        bars = _wave_bars(symbol, Direction.LONG)
        state.seed(symbol, bars)
        buffer = state.get(symbol)
        assert buffer is not None
        price = bars[-1].close + 0.5
        buffer.current_kline = _bar(symbol, 120, bars[-1].close, price, closed=False)
        candidates.append(
            Candidate(
                snapshot=replace(
                    _snapshot(symbol, Direction.LONG, price), score=90 - index
                ),
                selected=True,
            )
        )
    validator = DeepEntryValidator(state, StageCSettings(max_candidates=3))
    assert len(validator.validate(candidates)) == 3
    assert validator.last_processed_count == 3


def test_missing_optional_depth_and_oi_is_neutral_and_does_not_crash() -> None:
    state, candidate = _stage_c_case(Direction.LONG, "clean", deep=False)
    result = DeepEntryValidator(
        state,
        StageCSettings(late_score_threshold=95, exhaustion_threshold=95),
    ).validate([candidate])[0]
    analysis = result.snapshot.entry_analysis
    assert analysis is not None
    assert {"DEPTH", "OI"} <= set(analysis.optional_data_missing)


def test_optional_depth_and_oi_do_not_block_stage_c_watch_gate() -> None:
    state, candidate = _stage_c_case(Direction.LONG, "clean", deep=False)
    settings = StageCSettings(
        require_for_watch=True,
        late_score_threshold=95,
        exhaustion_threshold=95,
        min_entry_quality=40,
        min_room_pct=0.01,
        min_rr=0.1,
    )
    validated = DeepEntryValidator(state, settings).validate([candidate])[0]
    analysis = validated.snapshot.entry_analysis
    assert analysis is not None
    allowed = replace(
        validated,
        snapshot=replace(
            validated.snapshot,
            entry_analysis=replace(
                analysis,
                final_decision=EntryDecision.ENTER_CANDIDATE,
                entry_quality=80,
            ),
        ),
    )
    assert allowed.snapshot.trade_data_ready is True
    assert allowed.snapshot.deep_data_ready is False
    fsm = SignalFSM(state, ScannerSettings(), LateSettings(), stage_c_settings=settings)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert fsm.advance(allowed, now) is not None
    watch = fsm.advance(allowed, now + timedelta(seconds=1))
    assert watch is not None and watch.to_state is SignalState.WATCH


def test_stage_c_replay_is_deterministic() -> None:
    state, candidate = _stage_c_case(Direction.LONG, "retest")
    validator = DeepEntryValidator(
        state,
        StageCSettings(late_score_threshold=95, exhaustion_threshold=95),
    )
    first = validator.validate([candidate])[0].snapshot.entry_analysis
    second = validator.validate([candidate])[0].snapshot.entry_analysis
    assert first is not None and second is not None
    assert asdict(first) == asdict(second)


def test_stage_c_wait_does_not_advance_fsm_or_create_signal_spam() -> None:
    state, candidate = _stage_c_case(Direction.LONG, "clean")
    stage_c = StageCSettings(
        require_for_watch=True,
        late_score_threshold=95,
        exhaustion_threshold=95,
        min_entry_quality=50,
        min_room_pct=0.01,
        min_rr=0.1,
    )
    validated = DeepEntryValidator(state, stage_c).validate([candidate])[0]
    analysis = validated.snapshot.entry_analysis
    assert analysis is not None and analysis.final_decision is EntryDecision.WAIT_RETEST
    fsm = SignalFSM(state, ScannerSettings(), LateSettings(), stage_c_settings=stage_c)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = fsm.advance(validated, now)
    assert first is not None and first.to_state is SignalState.CANDIDATE
    assert fsm.advance(validated, now + timedelta(seconds=1)) is None

    allowed_analysis = replace(
        analysis,
        final_decision=EntryDecision.ENTER_CANDIDATE,
        retest_state=RetestState.HELD,
        retest_started=True,
        retest_held=True,
    )
    allowed = replace(
        validated,
        snapshot=replace(validated.snapshot, entry_analysis=allowed_analysis),
    )
    transition = fsm.advance(allowed, now + timedelta(seconds=2))
    assert transition is not None and transition.to_state is SignalState.WATCH


async def test_stage_c_snapshot_is_persisted_and_explainable() -> None:
    state, candidate = _stage_c_case(Direction.LONG, "retest")
    settings = StageCSettings(
        late_score_threshold=95,
        exhaustion_threshold=95,
        min_room_pct=0.01,
        min_rr=0.1,
    )
    validated = DeepEntryValidator(state, settings).validate([candidate])[0]
    analysis = validated.snapshot.entry_analysis
    assert analysis is not None
    transition = SignalTransition(
        symbol=validated.snapshot.symbol,
        direction=validated.snapshot.direction,
        from_state=SignalState.CANDIDATE,
        to_state=SignalState.WATCH,
        reason_code="WATCH_THRESHOLD",
        reason_text="watch threshold",
        timestamp=validated.snapshot.timestamp,
        snapshot=validated.snapshot,
        levels=None,
    )
    database = Database("sqlite+aiosqlite:///:memory:")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with database.session() as session, session.begin():
            await SignalRepository(session).persist_transition(transition)
        async with database.session() as session:
            signal = await session.scalar(select(SignalRecord))
            feature = await session.scalar(select(SignalFeatureRecord))
        assert signal is not None and signal.final_decision == "ENTER_CANDIDATE"
        assert float(signal.entry_quality or 0) == analysis.entry_quality
        assert feature is not None and feature.stage_c_json is not None
        assert feature.final_decision == "ENTER_CANDIDATE"
        assert "RETEST_HELD" in feature.reason_codes_json
        message = format_signal(signal, feature.features_json)
        explanation = format_signal_explanation(signal, feature.features_json)
        assert "Entry Quality" in message
        assert "R:R" in message
        assert "Почему" in explanation
        assert "не команда на вход" in explanation
    finally:
        await database.dispose()
