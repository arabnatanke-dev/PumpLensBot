"""EARLY radar scenarios and shadow persistence. / Сценарии и shadow-хранение EARLY."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from pumplens.analytics.buffers import MarketState
from pumplens.analytics.early_stats import EarlyStatRow, calculate_early_statistics
from pumplens.analytics.fsm import SignalFSM, SignalTransition
from pumplens.analytics.scoring import score_stage_a
from pumplens.analytics.stage_a import StageAScanner, evaluate_early
from pumplens.config import EarlySettings, LateSettings, ScannerSettings
from pumplens.domain.enums import DataQuality, Direction, SignalState
from pumplens.domain.events import TickerEvent
from pumplens.domain.models import Candidate, FeatureSnapshot, Kline, Ticker24h
from pumplens.replay.early_outcomes import EarlySample, evaluate_early_samples
from pumplens.replay.reader import ReplayReader
from pumplens.replay.recorder import MarketEventRecorder
from pumplens.storage.db import Database
from pumplens.storage.models import (
    Base,
    DeliveryRecord,
    SignalFeatureRecord,
    SignalRecord,
)
from pumplens.storage.repositories import UserRepository
from pumplens.telegram.notifications import TransitionFanout


def early_snapshot(
    direction: Direction = Direction.LONG,
    *,
    timestamp: datetime | None = None,
    quote_volume_1m: float = 20_000,
    trade_count_1m: int = 50,
) -> FeatureSnapshot:
    sign = 1.0 if direction is Direction.LONG else -1.0
    snapshot = FeatureSnapshot(
        symbol="EARLYUSDT",
        timestamp=timestamp or datetime.now(UTC),
        direction=direction,
        last_price=100.42 if direction is Direction.LONG else 99.58,
        return_1m=0.42 * sign,
        return_3m=0.5 * sign,
        return_5m=0.8 * sign,
        return_15m=1.0 * sign,
        acceleration_1m=0.1 * sign,
        volume_ratio_1m=5.0,
        volume_robust_z=4.0,
        buy_pressure=0.72 if direction is Direction.LONG else 0.28,
        trade_rate_ratio=5.0,
        spread_pct=0.05,
        relative_strength_1m=0.2 * sign,
        range_pct_1m=0.7,
        candle_structure=0.7,
        quote_volume_24h=30_000_000,
        quote_volume_1m=quote_volume_1m,
        trade_count_1m=trade_count_1m,
        score=0,
        data_quality=DataQuality.FRESH,
    )
    return score_stage_a(snapshot, ScannerSettings().max_spread_pct)


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_long_and_short_early(direction: Direction) -> None:
    snapshot = early_snapshot(direction)
    accepted, tier = evaluate_early(snapshot, None, ScannerSettings(), EarlySettings())
    assert 58 <= snapshot.score < 70
    assert accepted is True
    assert tier == "mid"


def test_low_activity_ratio_spike_is_rejected() -> None:
    snapshot = early_snapshot(quote_volume_1m=500, trade_count_1m=5)
    accepted, _ = evaluate_early(snapshot, None, ScannerSettings(), EarlySettings())
    assert snapshot.volume_ratio_1m >= 5
    assert snapshot.trade_rate_ratio >= 5
    assert accepted is False


class SequenceFeatureEngine:
    def __init__(self, snapshots: list[FeatureSnapshot]) -> None:
        self._snapshots = iter(snapshots)

    def snapshot_all(self) -> list[FeatureSnapshot]:
        return [next(self._snapshots)]


def test_early_requires_two_hits_inside_three_seconds() -> None:
    now = datetime.now(UTC)
    scanner = StageAScanner(
        SequenceFeatureEngine(
            [
                early_snapshot(timestamp=now),
                early_snapshot(timestamp=now + timedelta(seconds=1)),
            ]
        ),  # type: ignore[arg-type]
        ScannerSettings(),
        LateSettings(),
        EarlySettings(),
    )
    assert scanner.scan_once()[0].early_selected is False
    assert scanner.scan_once()[0].early_selected is True


def seeded_state() -> MarketState:
    state = MarketState(["EARLYUSDT"])
    state.seed(
        "EARLYUSDT",
        [
            Kline(
                "EARLYUSDT",
                index * 60_000,
                index * 60_000 + 59_999,
                100,
                101,
                99,
                100.4,
                1,
                100,
                10,
                70,
            )
            for index in range(3)
        ],
    )
    return state


def early_candidate(*, selected: bool, snapshot: FeatureSnapshot | None = None) -> Candidate:
    item = snapshot or early_snapshot()
    return Candidate(
        snapshot=item,
        selected=True,
        confirmations=("price", "volume", "pressure", "trade_rate"),
        early_selected=selected,
        early_liquidity_tier="mid" if selected else None,
        early_snapshot=item if selected else None,
    )


def enter_early(fsm: SignalFSM, now: datetime) -> None:
    candidate_transition = fsm.advance(early_candidate(selected=False), now)
    early_transition = fsm.advance(early_candidate(selected=True), now + timedelta(seconds=1))
    assert candidate_transition is not None
    assert candidate_transition.to_state is SignalState.CANDIDATE
    assert early_transition is not None
    assert early_transition.to_state is SignalState.EARLY


def test_early_upgrades_to_watch_without_changing_watch_gate() -> None:
    fsm = SignalFSM(seeded_state(), ScannerSettings(), LateSettings(), EarlySettings())
    now = datetime.now(UTC)
    enter_early(fsm, now)
    watch_snapshot = replace(early_snapshot(), score=75, deep_data_ready=True)
    transition = fsm.advance(
        early_candidate(selected=False, snapshot=watch_snapshot),
        now + timedelta(seconds=2),
    )
    assert transition is not None
    assert transition.from_state is SignalState.EARLY
    assert transition.to_state is SignalState.WATCH


def test_early_invalidates_after_short_weak_period() -> None:
    settings = EarlySettings(invalidate_after_seconds=10)
    fsm = SignalFSM(seeded_state(), ScannerSettings(), LateSettings(), settings)
    now = datetime.now(UTC)
    enter_early(fsm, now)
    weak = early_candidate(selected=False)
    assert fsm.advance(weak, now + timedelta(seconds=2)) is None
    transition = fsm.advance(weak, now + timedelta(seconds=12))
    assert transition is not None
    assert transition.to_state is SignalState.INVALIDATED
    assert transition.reason_code == "EARLY_WEAK"


def test_early_becomes_too_late_at_early_limit() -> None:
    fsm = SignalFSM(seeded_state(), ScannerSettings(), LateSettings(), EarlySettings())
    now = datetime.now(UTC)
    enter_early(fsm, now)
    moved = replace(early_snapshot(), return_1m=1.51)
    transition = fsm.advance(
        early_candidate(selected=False, snapshot=moved),
        now + timedelta(seconds=2),
    )
    assert transition is not None
    assert transition.to_state is SignalState.TOO_LATE
    assert transition.reason_code == "EARLY_LIMIT_CROSSED"


def test_false_early_rearms_after_short_cooldown() -> None:
    settings = EarlySettings(invalidate_after_seconds=10, rearm_seconds=60)
    fsm = SignalFSM(seeded_state(), ScannerSettings(), LateSettings(), settings)
    now = datetime.now(UTC)
    enter_early(fsm, now)
    weak = early_candidate(selected=False)
    fsm.advance(weak, now + timedelta(seconds=2))
    invalidated = fsm.advance(weak, now + timedelta(seconds=12))
    assert invalidated is not None and invalidated.to_state is SignalState.INVALIDATED
    cooldown = fsm.advance(weak, now + timedelta(seconds=13))
    assert cooldown is not None and cooldown.to_state is SignalState.COOLDOWN
    assert fsm.advance(weak, now + timedelta(seconds=71)) is None
    rearmed = fsm.advance(weak, now + timedelta(seconds=72))
    assert rearmed is not None and rearmed.to_state is SignalState.NORMAL


@pytest.fixture
async def database() -> Database:
    db = Database("sqlite+aiosqlite:///:memory:")
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.dispose()


async def test_shadow_early_is_persisted_without_delivery(database: Database) -> None:
    now = datetime.now(UTC)
    snapshot = early_snapshot(timestamp=now)
    candidate_transition = SignalTransition(
        symbol=snapshot.symbol,
        direction=snapshot.direction,
        from_state=SignalState.NORMAL,
        to_state=SignalState.CANDIDATE,
        reason_code="STAGE_A_DEBOUNCED",
        reason_text="stage a debounced",
        timestamp=now,
        snapshot=snapshot,
        levels=None,
    )
    early_transition = replace(
        candidate_transition,
        from_state=SignalState.CANDIDATE,
        to_state=SignalState.EARLY,
        reason_code="EARLY_FLASH_DEBOUNCED",
        reason_text="early flash debounced",
        timestamp=now + timedelta(seconds=1),
        liquidity_tier="mid",
    )
    fanout = TransitionFanout(database, early_shadow_mode=True)
    await fanout(candidate_transition)
    await fanout(early_transition)

    async with database.session() as session:
        signal = await session.scalar(select(SignalRecord))
        feature = await session.scalar(
            select(SignalFeatureRecord).order_by(SignalFeatureRecord.ts.desc())
        )
        deliveries = await session.scalar(select(func.count()).select_from(DeliveryRecord))
    assert signal is not None and signal.early_at is not None
    assert signal.early_at.replace(tzinfo=UTC) == early_transition.timestamp
    assert float(signal.early_price or 0) == snapshot.last_price
    assert signal.early_liquidity_tier == "mid"
    assert feature is not None and feature.features_json["trade_count_1m"] == 50
    assert deliveries == 0


async def test_visible_early_chain_edits_one_delivery_thread(database: Database) -> None:
    async with database.session() as session, session.begin():
        user = await UserRepository(session).get_or_create(
            telegram_user_id=42,
            chat_id=42,
            display_name="Rose",
            language="ru",
        )
        user.status = "ACTIVE"

    now = datetime.now(UTC)
    snapshot = early_snapshot(timestamp=now)
    candidate_transition = SignalTransition(
        symbol=snapshot.symbol,
        direction=snapshot.direction,
        from_state=SignalState.NORMAL,
        to_state=SignalState.CANDIDATE,
        reason_code="STAGE_A_DEBOUNCED",
        reason_text="stage a debounced",
        timestamp=now,
        snapshot=snapshot,
        levels=None,
    )
    early_transition = replace(
        candidate_transition,
        from_state=SignalState.CANDIDATE,
        to_state=SignalState.EARLY,
        reason_code="EARLY_FLASH_DEBOUNCED",
        timestamp=now + timedelta(seconds=1),
        liquidity_tier="mid",
    )
    watch_transition = replace(
        early_transition,
        from_state=SignalState.EARLY,
        to_state=SignalState.WATCH,
        reason_code="WATCH_THRESHOLD",
        timestamp=now + timedelta(seconds=10),
        snapshot=replace(snapshot, score=75, deep_data_ready=True),
    )
    confirmed_transition = replace(
        watch_transition,
        from_state=SignalState.WATCH,
        to_state=SignalState.CONFIRMED,
        reason_code="SCORE_HELD_5S",
        timestamp=now + timedelta(seconds=20),
    )
    fanout = TransitionFanout(database, early_shadow_mode=False)
    for transition in (
        candidate_transition,
        early_transition,
        watch_transition,
        confirmed_transition,
    ):
        await fanout(transition)

    async with database.session() as session:
        deliveries = list(
            await session.scalars(select(DeliveryRecord).order_by(DeliveryRecord.created_at))
        )
    assert [delivery.stage for delivery in deliveries] == ["EARLY", "WATCH", "CONFIRMED"]
    assert [delivery.action for delivery in deliveries] == ["SEND", "EDIT", "EDIT"]


def test_early_outcome_and_statistics() -> None:
    outcome = evaluate_early_samples(
        Direction.LONG,
        100,
        [
            EarlySample(15, 100.5),
            EarlySample(30, 99.8),
            EarlySample(60, 101.0),
            EarlySample(180, 102.0),
            EarlySample(300, 101.5),
        ],
    )
    assert outcome.prices[15] == 100.5
    assert outcome.mfe_pct == pytest.approx(2.0)
    assert outcome.mae_pct == pytest.approx(-0.2)

    now = datetime.now(UTC)
    rows = [
        EarlyStatRow(
            uuid.uuid4(),
            now,
            now + timedelta(seconds=20),
            now + timedelta(seconds=40),
            "mid",
            "LONG",
            100,
            False,
            price_15s=100.5,
            price_5m=101.5,
            mfe=2.0,
            mae=-0.2,
        ),
        EarlyStatRow(
            uuid.uuid4(),
            now + timedelta(minutes=30),
            None,
            None,
            "thin",
            "SHORT",
            100,
            True,
            price_15s=99.0,
            price_5m=101.0,
            mfe=1.0,
            mae=-1.0,
        ),
    ]
    stats = calculate_early_statistics(rows)
    assert stats.watch_conversion_pct == 50
    assert stats.confirmed_conversion_pct == 50
    assert stats.invalidated_pct == 50
    assert stats.average_watch_lead_seconds == 20
    assert stats.average_returns_pct["15s"] == pytest.approx(0.75)
    assert stats.liquidity_groups["thin"].invalidated_pct == 100


async def test_replay_reader_rebuilds_early_checkpoints(tmp_path: Path) -> None:
    path = tmp_path / "early.jsonl"
    recorder = MarketEventRecorder(path)
    await recorder.open()
    for seconds, price in ((15, 100.5), (30, 101.0), (60, 102.0), (300, 103.0)):
        await recorder.record(
            TickerEvent(
                Ticker24h(
                    symbol="EARLYUSDT",
                    last_price=price,
                    quote_volume=1_000_000,
                    price_change_pct=0,
                    event_time_ms=1_000_000 + seconds * 1_000,
                )
            )
        )
    await recorder.close()
    outcome = await ReplayReader(path).early_outcome(
        direction=Direction.LONG,
        symbol="EARLYUSDT",
        early_price=100,
        early_time_ms=1_000_000,
    )
    assert outcome.prices[15] == 100.5
    assert outcome.prices[300] == 103.0
    assert outcome.mfe_pct == pytest.approx(3.0)
