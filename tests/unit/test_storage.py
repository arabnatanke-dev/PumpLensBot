"""Database idempotency tests. / Тесты идемпотентности базы данных."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from pumplens.analytics.fsm import SignalTransition
from pumplens.domain.enums import DataQuality, Direction, SignalState
from pumplens.domain.models import FeatureSnapshot
from pumplens.storage.db import Database
from pumplens.storage.models import (
    Base,
    DeliveryRecord,
    SignalEventRecord,
    SignalRecord,
    UserPreferenceRecord,
    UserRecord,
)
from pumplens.storage.repositories import SignalRepository, UserRepository
from pumplens.telegram.notifications import TransitionFanout


@pytest.fixture
async def database() -> Database:
    db = Database("sqlite+aiosqlite:///:memory:")
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.dispose()


def feature_snapshot() -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol="TESTUSDT",
        timestamp=datetime.now(UTC),
        direction=Direction.LONG,
        last_price=1.0,
        return_1m=1,
        return_3m=1,
        return_5m=1,
        return_15m=1,
        acceleration_1m=1,
        volume_ratio_1m=3,
        volume_robust_z=4,
        buy_pressure=0.65,
        trade_rate_ratio=3,
        spread_pct=0.1,
        relative_strength_1m=1,
        range_pct_1m=1,
        candle_structure=0.8,
        quote_volume_24h=10_000_000,
        score=70,
        data_quality=DataQuality.FRESH,
    )


async def test_user_get_or_create_is_idempotent(database: Database) -> None:
    async with database.session() as session, session.begin():
        repository = UserRepository(session)
        first = await repository.get_or_create(
            telegram_user_id=42,
            chat_id=42,
            display_name="Rose",
            language="ru",
        )
        second = await repository.get_or_create(
            telegram_user_id=42,
            chat_id=43,
            display_name="Rose Updated",
            language="ru",
        )
        assert first.id == second.id

    async with database.session() as session:
        users = await session.scalar(select(func.count()).select_from(UserRecord))
        preferences = await session.scalar(
            select(func.count()).select_from(UserPreferenceRecord)
        )
        assert users == 1
        assert preferences == 1


async def test_signal_transition_is_persisted_once(database: Database) -> None:
    transition = SignalTransition(
        symbol="TESTUSDT",
        direction=Direction.LONG,
        from_state=SignalState.NORMAL,
        to_state=SignalState.CANDIDATE,
        reason_code="STAGE_A_DEBOUNCED",
        reason_text="stage a debounced",
        timestamp=datetime.now(UTC),
        snapshot=feature_snapshot(),
        levels=None,
    )
    async with database.session() as session, session.begin():
        signal_id = await SignalRepository(session).persist_transition(transition)

    async with database.session() as session:
        signal = await session.get(SignalRecord, signal_id)
        events = await session.scalar(select(func.count()).select_from(SignalEventRecord))
        assert signal is not None
        assert signal.state == SignalState.CANDIDATE.value
        assert events == 1


def test_schema_contains_all_mvp_tables() -> None:
    expected = {
        "symbols",
        "signal_runs",
        "signals",
        "signal_features",
        "signal_events",
        "signal_outcomes",
        "early_outcomes",
        "users",
        "user_consents",
        "user_preferences",
        "invite_codes",
        "exchange_accounts",
        "encrypted_credentials",
        "portfolio_snapshots",
        "spot_holdings",
        "earn_holdings",
        "funding_holdings",
        "positions",
        "risk_alerts",
        "deliveries",
        "service_health",
    }
    assert expected <= set(Base.metadata.tables)


async def test_watch_transition_creates_one_user_delivery(database: Database) -> None:
    async with database.session() as session, session.begin():
        user = await UserRepository(session).get_or_create(
            telegram_user_id=42,
            chat_id=42,
            display_name="Rose",
            language="ru",
        )
        user.status = "ACTIVE"

    now = datetime.now(UTC)
    candidate_transition = SignalTransition(
        symbol="TESTUSDT",
        direction=Direction.LONG,
        from_state=SignalState.NORMAL,
        to_state=SignalState.CANDIDATE,
        reason_code="STAGE_A_DEBOUNCED",
        reason_text="stage a debounced",
        timestamp=now,
        snapshot=feature_snapshot(),
        levels=None,
    )
    watch_transition = replace(
        candidate_transition,
        from_state=SignalState.CANDIDATE,
        to_state=SignalState.WATCH,
        reason_code="WATCH_THRESHOLD",
        reason_text="watch threshold",
        timestamp=now + timedelta(seconds=1),
    )
    fanout = TransitionFanout(database)
    await fanout(candidate_transition)
    await fanout(watch_transition)
    await fanout(watch_transition)

    async with database.session() as session:
        deliveries = list(await session.scalars(select(DeliveryRecord)))
        assert len(deliveries) == 1
        assert deliveries[0].stage == SignalState.WATCH.value


def test_migrations_upgrade_through_stage_c(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).parents[2]
    database_path = tmp_path / "migration.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    config = Config(root / "alembic.ini")
    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        user_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        delivery_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(deliveries)").fetchall()
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        snapshot_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(portfolio_snapshots)").fetchall()
        }
        signal_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(signals)").fetchall()
        }
        feature_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(signal_features)").fetchall()
        }
        outcome_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(signal_outcomes)").fetchall()
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"telegram_panel_message_id", "telegram_menu_message_id"} <= user_columns
    assert {"delete_after", "deleted_at", "cleanup_error"} <= delivery_columns
    assert {"earn_value", "funding_value", "source_status_json"} <= snapshot_columns
    assert {"earn_holdings", "funding_holdings"} <= tables
    assert {"entry_quality", "final_decision"} <= signal_columns
    assert {
        "stage_c_json",
        "entry_quality",
        "final_decision",
        "reason_codes_json",
    } <= feature_columns
    assert "last_sampled_at" in outcome_columns
    assert revision == ("0008_stage_c_entry_analysis",)


def test_existing_0006_database_upgrades_to_stage_c(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).parents[2]
    database_path = tmp_path / "migration-from-0006.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    config = Config(root / "alembic.ini")
    command.upgrade(config, "0006_full_portfolio")
    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        user_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        signal_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(signals)").fetchall()
        }
    assert revision == ("0008_stage_c_entry_analysis",)
    assert "telegram_menu_message_id" in user_columns
    assert {"earn_holdings", "funding_holdings"} <= tables
    assert {"entry_quality", "final_decision"} <= signal_columns


def test_existing_0007_database_upgrades_to_stage_c(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).parents[2]
    database_path = tmp_path / "migration-from-0007.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    config = Config(root / "alembic.ini")
    command.upgrade(config, "0007_telegram_reply_menu_anchor")
    # 0001 uses current metadata in this legacy project, so remove new columns to
    # reproduce a real production schema that stopped at 0007.
    # 0001 использует текущие metadata; удаляем новые колонки для честного пути 0007.
    with sqlite3.connect(database_path) as connection:
        for table, column in (
            ("signals", "entry_quality"),
            ("signals", "final_decision"),
            ("signal_features", "stage_c_json"),
            ("signal_features", "entry_quality"),
            ("signal_features", "final_decision"),
            ("signal_features", "reason_codes_json"),
            ("signal_outcomes", "last_sampled_at"),
        ):
            connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        feature_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(signal_features)").fetchall()
        }
        outcome_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(signal_outcomes)").fetchall()
        }
    assert revision == ("0008_stage_c_entry_analysis",)
    assert {"stage_c_json", "entry_quality", "final_decision"} <= feature_columns
    assert "last_sampled_at" in outcome_columns
