"""Deterministic personal-monitor tests. / Тесты персонального монитора."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from pumplens.analytics.buffers import MarketState
from pumplens.binance.ws_personal import PersonalMarketWebSocket
from pumplens.config import AppSettings, StageCSettings
from pumplens.domain.enums import DataQuality, Direction
from pumplens.domain.models import FeatureSnapshot, Kline
from pumplens.personal_monitor.analyzer import analysis_payload
from pumplens.personal_monitor.domain import EvaluationDecision, FullAnalysisResult
from pumplens.personal_monitor.evaluator import evaluate_scenario
from pumplens.personal_monitor.service import PersonalMonitorService, SymbolRuntime
from pumplens.storage.db import Database
from pumplens.storage.models import Base, MonitoredScenarioRecord, UserRecord
from pumplens.telegram.keyboards import personal_monitor_result_keyboard


def _scenario(direction: Direction) -> MonitoredScenarioRecord:
    return MonitoredScenarioRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        chat_id=42,
        symbol="ZKUSDT",
        direction=direction.value,
        setup_type="BREAK_RETEST",
        status="WAITING",
        timeframe="1m",
        trigger_level=Decimal("100"),
        retest_zone_low=Decimal("99"),
        retest_zone_high=Decimal("101"),
        invalidation_level=Decimal("105" if direction is Direction.SHORT else "95"),
        target1=Decimal("90" if direction is Direction.SHORT else "110"),
        require_volume_confirmation=True,
        require_pressure_confirmation=True,
        require_oi_confirmation=True,
        breakout_observed=True,
        retest_observed=False,
        analysis_json={},
        is_active=True,
    )


@pytest.fixture
async def database() -> Database:
    db = Database("sqlite+aiosqlite:///:memory:")
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.dispose()


def _snapshot(direction: Direction) -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol="ZKUSDT",
        timestamp=datetime.now(UTC),
        direction=direction,
        last_price=99 if direction is Direction.SHORT else 101,
        return_1m=-1 if direction is Direction.SHORT else 1,
        return_3m=-1 if direction is Direction.SHORT else 1,
        return_5m=-1 if direction is Direction.SHORT else 1,
        return_15m=-1 if direction is Direction.SHORT else 1,
        acceleration_1m=1,
        volume_ratio_1m=3,
        volume_robust_z=4,
        buy_pressure=0.30 if direction is Direction.SHORT else 0.70,
        trade_rate_ratio=3,
        spread_pct=0.05,
        relative_strength_1m=1,
        range_pct_1m=1,
        candle_structure=0.8,
        quote_volume_24h=10_000_000,
        oi_delta_pct=-0.10 if direction is Direction.SHORT else 0.10,
        oi_data_ready=True,
        data_quality=DataQuality.FRESH,
    )


def _candle(direction: Direction, *, closed: bool = True) -> Kline:
    return Kline(
        symbol="ZKUSDT",
        open_time_ms=1_000,
        close_time_ms=60_999,
        open=100,
        high=101,
        low=99,
        close=99 if direction is Direction.SHORT else 101,
        base_volume=1,
        quote_volume=10_000,
        trade_count=100,
        taker_buy_quote_volume=3_000,
        closed=closed,
    )


@pytest.mark.parametrize("direction", list(Direction))
def test_break_retest_is_mirrored_and_deterministic(direction: Direction) -> None:
    scenario = _scenario(direction)
    decision, reason = evaluate_scenario(
        scenario,
        _candle(direction),
        _snapshot(direction),
        StageCSettings(),
    )
    assert decision is EvaluationDecision.CONFIRMED
    assert reason == "CLOSED_CANDLE_CONFIRMED"
    assert scenario.retest_observed is True


def test_open_candle_never_changes_scenario() -> None:
    scenario = _scenario(Direction.SHORT)
    decision, reason = evaluate_scenario(
        scenario,
        _candle(Direction.SHORT, closed=False),
        _snapshot(Direction.SHORT),
        StageCSettings(),
    )
    assert decision is EvaluationDecision.WAIT
    assert reason == "CANDLE_NOT_CLOSED"
    assert scenario.retest_observed is False


@pytest.mark.parametrize(
    ("direction", "close"),
    [(Direction.LONG, 94), (Direction.SHORT, 106)],
)
def test_close_beyond_invalidation_never_auto_flips(
    direction: Direction,
    close: float,
) -> None:
    scenario = _scenario(direction)
    candle = _candle(direction)
    candle = replace(candle, close=close)
    decision, reason = evaluate_scenario(
        scenario,
        candle,
        _snapshot(direction),
        StageCSettings(),
    )
    assert decision is EvaluationDecision.INVALIDATED
    assert reason == "CLOSE_BEYOND_INVALIDATION"
    assert scenario.direction == direction.value


def test_oi_requirement_remains_waiting_when_oi_is_missing() -> None:
    scenario = _scenario(Direction.LONG)
    snapshot = _snapshot(Direction.LONG)
    snapshot = replace(snapshot, oi_data_ready=False)
    decision, _ = evaluate_scenario(
        scenario,
        _candle(Direction.LONG),
        snapshot,
        StageCSettings(),
    )
    assert decision is EvaluationDecision.WAIT


def test_personal_websocket_routes_only_selected_symbol_streams() -> None:
    assert PersonalMarketWebSocket.route_url(
        "wss://fstream.binance.com", "market"
    ) == "wss://fstream.binance.com/market/ws"
    assert PersonalMarketWebSocket.route_url(
        "wss://fstream.binance.com", "public"
    ) == "wss://fstream.binance.com/public/ws"
    assert PersonalMarketWebSocket._streams({"ZKUSDT"}, ("kline_1m", "aggTrade")) == [
        "zkusdt@kline_1m",
        "zkusdt@aggTrade",
    ]


def test_no_trade_result_offers_continue_observing() -> None:
    scenario = _scenario(Direction.LONG)
    scenario.direction = None
    scenario.setup_type = None
    scenario.status = "SEARCHING"
    buttons = [
        button
        for row in personal_monitor_result_keyboard(scenario).inline_keyboard
        for button in row
    ]
    assert any(button.text == "🔄 Продолжить наблюдение" for button in buttons)
    assert any(button.callback_data == f"monitor:activate:{scenario.id.hex}" for button in buttons)


def test_full_feature_snapshot_is_json_serializable() -> None:
    long_snapshot = _snapshot(Direction.LONG)
    short_snapshot = _snapshot(Direction.SHORT)
    result = FullAnalysisResult(
        symbol="ZKUSDT",
        price=100,
        long_score=61,
        short_score=72,
        long_snapshot=long_snapshot,
        short_snapshot=short_snapshot,
        long_analysis=None,
        short_analysis=None,
        plan=None,
        data_quality="FRESH",
    )
    payload = analysis_payload(result)
    assert payload["long_snapshot"]["timestamp"] == long_snapshot.timestamp.isoformat()
    assert payload["short_snapshot"]["direction"] == "SHORT"
    json.dumps(payload)


async def test_same_closed_candle_is_not_analyzed_twice(database: Database) -> None:
    async with database.session() as session, session.begin():
        user = UserRecord(
            telegram_user_id=42,
            chat_id=42,
            status="ACTIVE",
            onboarding_state="COMPLETE",
        )
        session.add(user)
        await session.flush()
        scenario = _scenario(Direction.LONG)
        scenario.user_id = user.id
        scenario.require_oi_confirmation = False
        session.add(scenario)

    long_snapshot = replace(_snapshot(Direction.LONG), volume_ratio_1m=1)
    result = FullAnalysisResult(
        symbol="ZKUSDT",
        price=101,
        long_score=61,
        short_score=20,
        long_snapshot=long_snapshot,
        short_snapshot=_snapshot(Direction.SHORT),
        long_analysis=None,
        short_analysis=None,
        plan=None,
        data_quality="FRESH",
    )

    class FakeAnalyzer:
        calls = 0

        def analyze(self, _symbol: str) -> FullAnalysisResult:
            self.calls += 1
            return result

    class FakeRest:
        calls = 0

        async def open_interest(self, _symbol: str) -> object:
            self.calls += 1
            raise RuntimeError("optional OI unavailable")

        async def aclose(self) -> None:
            return None

    bot = type("FakeBot", (), {"send_message": None})()
    service = PersonalMonitorService(database, bot, AppSettings())  # type: ignore[arg-type]
    fake_analyzer = FakeAnalyzer()
    fake_rest = FakeRest()
    await service._rest.aclose()
    service._rest = fake_rest  # type: ignore[assignment]
    ready = asyncio.Event()
    ready.set()
    service._runtimes["ZKUSDT"] = SymbolRuntime(
        state=MarketState(["ZKUSDT"]),
        analyzer=fake_analyzer,  # type: ignore[arg-type]
        lock=asyncio.Lock(),
        ready=ready,
    )
    candle = _candle(Direction.LONG)
    await service._process_closed_candle("ZKUSDT", candle)
    await service._process_closed_candle("ZKUSDT", candle)

    assert fake_analyzer.calls == 1
    assert fake_rest.calls == 1
    async with database.session() as session:
        stored = await session.scalar(select(MonitoredScenarioRecord))
        assert stored is not None
        assert stored.is_active is True
        assert stored.last_evaluated_candle_open_time == candle.open_time_ms
    await service.close()
