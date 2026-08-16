"""Clean Telegram panel and cleanup tests. / Тесты чистой панели и cleanup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject
from aiogram.methods import DeleteMessage, EditMessageText
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from sqlalchemy import select

from pumplens.config import AppSettings, RuntimeSecrets
from pumplens.service_state import ServiceState
from pumplens.storage.db import Database
from pumplens.storage.models import (
    Base,
    DeliveryRecord,
    ExchangeAccountRecord,
    PortfolioSnapshotRecord,
    RiskAlertRecord,
    SignalRecord,
    SpotHoldingRecord,
    UserRecord,
)
from pumplens.telegram.clean_ui import (
    _edit_callback,
    _history_text,
    _refresh_portfolio,
    clean_reply_menu_handler,
    clean_screen_callback,
    clean_start_handler,
)
from pumplens.telegram.keyboards import (
    early_keyboard,
    main_reply_keyboard,
    panel_binance_keyboard,
    panel_settings_keyboard,
    portfolio_keyboard,
    signals_keyboard,
    top_level_keyboard,
    welcome_keyboard,
)
from pumplens.telegram.notifications import DeliveryCleanupWorker, DeliveryWorker
from pumplens.telegram.panel import delete_command_best_effort, show_or_edit_panel
from pumplens.webapp.sessions import ConnectSessionStore


@pytest.fixture
async def database() -> Database:
    db = Database("sqlite+aiosqlite:///:memory:")
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.dispose()


class FakeBot:
    def __init__(self, *, edit_error: Exception | None = None) -> None:
        self.edit_error = edit_error
        self.edits: list[int] = []
        self.edit_markups: list[object] = []
        self.sent: list[int] = []
        self.sent_markups: list[object] = []
        self.deleted: list[tuple[int, int]] = []
        self.next_message_id = 900

    async def edit_message_text(self, _text: str, **kwargs: object) -> None:
        if self.edit_error:
            raise self.edit_error
        self.edits.append(int(kwargs["message_id"]))
        self.edit_markups.append(kwargs.get("reply_markup"))

    async def send_message(self, chat_id: int, _text: str, **_kwargs: object) -> object:
        self.sent.append(chat_id)
        self.sent_markups.append(_kwargs.get("reply_markup"))
        message = SimpleNamespace(message_id=self.next_message_id)
        self.next_message_id += 1
        return message

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))


async def _user(
    database: Database,
    *,
    panel_id: int | None = None,
    menu_id: int | None = None,
) -> UserRecord:
    async with database.session() as session, session.begin():
        user = UserRecord(
            telegram_user_id=42,
            chat_id=42,
            status="ACTIVE",
            onboarding_state="COMPLETE",
            telegram_panel_message_id=panel_id,
            telegram_menu_message_id=menu_id,
        )
        session.add(user)
        await session.flush()
        return user


def test_main_reply_keyboard_contract() -> None:
    keyboard = main_reply_keyboard()
    assert [[button.text for button in row] for row in keyboard.keyboard] == [
        ["💼 Портфель", "📈 Сигналы"],
        ["📊 Позиции", "🧪 EARLY"],
        ["⚙️ Настройки", "🔗 Binance"],
    ]
    assert keyboard.is_persistent is True
    assert keyboard.resize_keyboard is True


def _inline_buttons(keyboard: InlineKeyboardMarkup) -> list[object]:
    return [button for row in keyboard.inline_keyboard for button in row]


def test_top_level_screens_do_not_have_generic_back() -> None:
    keyboards = [
        portfolio_keyboard("overview"),
        signals_keyboard("all"),
        top_level_keyboard(),
        early_keyboard(),
        panel_settings_keyboard(
            profile="balanced",
            directions=["LONG", "SHORT"],
            connected=False,
            connect_url=None,
        ),
        panel_binance_keyboard(connected=False, connect_url=None),
    ]

    for keyboard in keyboards:
        buttons = _inline_buttons(keyboard)
        assert all(getattr(button, "text", None) != "⬅️ Назад" for button in buttons)
        assert all(getattr(button, "callback_data", None) != "menu:back" for button in buttons)

    overview_buttons = _inline_buttons(portfolio_keyboard("overview"))
    assert all(
        getattr(button, "text", None) != "⬅️ Обзор портфеля"
        for button in overview_buttons
    )


@pytest.mark.parametrize("section", ["spot", "futures", "earn", "funding"])
def test_portfolio_child_screens_have_parent_navigation(section: str) -> None:
    buttons = _inline_buttons(portfolio_keyboard(section))
    parent_buttons = [
        button
        for button in buttons
        if getattr(button, "text", None) == "⬅️ Обзор портфеля"
    ]
    assert len(parent_buttons) == 1
    assert getattr(parent_buttons[0], "callback_data", None) == "screen:portfolio:overview"


async def test_panel_edits_saved_message_instead_of_sending(database: Database) -> None:
    await _user(database, panel_id=77)
    bot = FakeBot()
    result = await show_or_edit_panel(
        cast(Bot, cast(Any, bot)),
        database,
        telegram_user_id=42,
        chat_id=42,
        text="panel",
        reply_markup=welcome_keyboard(),
    )
    assert result == 77
    assert bot.edits == [77]
    assert bot.sent == []


async def test_panel_falls_back_to_new_message_and_persists_it(database: Database) -> None:
    await _user(database, panel_id=77)
    error = TelegramBadRequest(
        method=EditMessageText(chat_id=42, message_id=77, text="panel"),
        message="message to edit not found",
    )
    bot = FakeBot(edit_error=error)
    result = await show_or_edit_panel(
        cast(Bot, cast(Any, bot)),
        database,
        telegram_user_id=42,
        chat_id=42,
        text="panel",
        reply_markup=welcome_keyboard(),
    )
    assert result == 900
    assert bot.sent == [42]
    async with database.session() as session:
        user = await session.scalar(select(UserRecord).where(UserRecord.telegram_user_id == 42))
        assert user is not None and user.telegram_panel_message_id == 900


async def test_slash_command_delete_is_best_effort() -> None:
    message = cast(Any, SimpleNamespace(chat=SimpleNamespace(id=42), message_id=12))
    bot = FakeBot()
    await delete_command_best_effort(cast(Bot, cast(Any, bot)), message)
    assert bot.deleted == [(42, 12)]

    async def fail_delete(_chat_id: int, _message_id: int) -> None:
        raise RuntimeError("no rights")

    bot.delete_message = fail_delete  # type: ignore[method-assign]
    await delete_command_best_effort(cast(Bot, cast(Any, bot)), message)


async def test_onboarding_edits_the_same_message(database: Database) -> None:
    await _user(database, panel_id=55)
    bot = FakeBot()
    callback = cast(
        Any,
        SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(chat=SimpleNamespace(id=42), message_id=55),
        ),
    )
    await _edit_callback(
        callback,
        cast(Bot, cast(Any, bot)),
        database,
        "next",
        welcome_keyboard(),
    )
    assert bot.edits == [55]
    assert bot.sent == []


async def test_reply_button_moves_screen_to_bottom_then_inline_edits_it(
    database: Database,
) -> None:
    await _user(database, panel_id=77, menu_id=55)
    bot = FakeBot()
    message = cast(
        Any,
        SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            chat=SimpleNamespace(id=42),
            message_id=66,
            text="💼 Портфель",
        ),
    )
    await clean_reply_menu_handler(
        message,
        cast(Bot, cast(Any, bot)),
        database,
        AppSettings(),
        RuntimeSecrets(),
        ConnectSessionStore(),
        ServiceState(),
    )
    assert bot.deleted == [(42, 66), (42, 77)]
    assert (42, 55) not in bot.deleted
    assert bot.edits == []
    assert bot.sent == [42]
    assert isinstance(bot.sent_markups[0], InlineKeyboardMarkup)
    async with database.session() as session:
        user = await session.scalar(
            select(UserRecord).where(UserRecord.telegram_user_id == 42)
        )
        assert user is not None and user.telegram_panel_message_id == 900
        assert user.telegram_menu_message_id == 55

    callback = cast(
        Any,
        SimpleNamespace(
            data="screen:portfolio:spot",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(chat=SimpleNamespace(id=42), message_id=900),
            answer=AsyncMock(),
        ),
    )
    await clean_screen_callback(
        callback,
        cast(Bot, cast(Any, bot)),
        database,
        AppSettings(),
        RuntimeSecrets(),
        ConnectSessionStore(),
        ServiceState(),
        cast(Any, None),
    )
    assert bot.edits == [900]
    assert bot.sent == [42]

    parent_callback = cast(
        Any,
        SimpleNamespace(
            data="screen:portfolio:overview",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(chat=SimpleNamespace(id=42), message_id=900),
            answer=AsyncMock(),
        ),
    )
    await clean_screen_callback(
        parent_callback,
        cast(Bot, cast(Any, bot)),
        database,
        AppSettings(),
        RuntimeSecrets(),
        ConnectSessionStore(),
        ServiceState(),
        cast(Any, None),
    )
    assert bot.edits == [900, 900]
    assert bot.sent == [42]
    assert bot.deleted == [(42, 66), (42, 77)]
    assert (42, 55) not in bot.deleted
    assert isinstance(bot.edit_markups[-1], InlineKeyboardMarkup)
    assert all(
        getattr(button, "text", None) != "⬅️ Обзор портфеля"
        for button in _inline_buttons(cast(InlineKeyboardMarkup, bot.edit_markups[-1]))
    )
    async with database.session() as session:
        user = await session.scalar(
            select(UserRecord).where(UserRecord.telegram_user_id == 42)
        )
        assert user is not None and user.telegram_panel_message_id == 900
        assert user.telegram_menu_message_id == 55


async def test_completed_start_creates_separate_menu_anchor_and_content_screen(
    database: Database,
) -> None:
    # Production migration path: the old release stored its keyboard message as panel.
    # Production migration: старая версия хранила keyboard message как panel.
    await _user(database, panel_id=77)
    bot = FakeBot()
    message = cast(
        Any,
        SimpleNamespace(
            from_user=SimpleNamespace(
                id=42,
                full_name="Rose",
                language_code="ru",
            ),
            chat=SimpleNamespace(id=42),
            message_id=67,
        ),
    )
    await clean_start_handler(
        message,
        CommandObject(prefix="/", command="start", mention=None),
        cast(Bot, cast(Any, bot)),
        database,
        RuntimeSecrets(),
        AppSettings.model_validate({"onboarding": {"invite_only": False}}),
    )
    assert bot.deleted == [(42, 67), (42, 77)]
    assert bot.edits == []
    assert bot.sent == [42, 42]
    assert bot.sent_markups == [main_reply_keyboard(), None]
    assert isinstance(bot.sent_markups[0], ReplyKeyboardMarkup)
    async with database.session() as session:
        user = await session.scalar(
            select(UserRecord).where(UserRecord.telegram_user_id == 42)
        )
        assert user is not None and user.telegram_menu_message_id == 900
        assert user.telegram_panel_message_id == 901
        assert user.telegram_menu_message_id != user.telegram_panel_message_id


async def test_repeated_start_replaces_anchor_without_accumulating(database: Database) -> None:
    await _user(database, panel_id=77)
    bot = FakeBot()

    def start_message(message_id: int) -> object:
        return SimpleNamespace(
            from_user=SimpleNamespace(id=42, full_name="Rose", language_code="ru"),
            chat=SimpleNamespace(id=42),
            message_id=message_id,
        )

    command = CommandObject(prefix="/", command="start", mention=None)
    for message_id in (67, 68):
        await clean_start_handler(
            cast(Any, start_message(message_id)),
            command,
            cast(Bot, cast(Any, bot)),
            database,
            RuntimeSecrets(),
            AppSettings.model_validate({"onboarding": {"invite_only": False}}),
        )

    assert bot.sent == [42, 42, 42, 42]
    assert (42, 900) in bot.deleted
    assert (42, 901) in bot.deleted
    async with database.session() as session:
        user = await session.scalar(
            select(UserRecord).where(UserRecord.telegram_user_id == 42)
        )
        assert user is not None and user.telegram_menu_message_id == 902
        assert user.telegram_panel_message_id == 903


async def test_reply_navigation_survives_old_screen_delete_failure(
    database: Database,
) -> None:
    await _user(database, panel_id=77, menu_id=55)
    bot = FakeBot()

    async def fail_delete(_chat_id: int, _message_id: int) -> None:
        raise RuntimeError("cannot delete")

    bot.delete_message = fail_delete  # type: ignore[method-assign]
    message = cast(
        Any,
        SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            chat=SimpleNamespace(id=42),
            message_id=66,
            text="💼 Портфель",
        ),
    )
    await clean_reply_menu_handler(
        message,
        cast(Bot, cast(Any, bot)),
        database,
        AppSettings(),
        RuntimeSecrets(),
        ConnectSessionStore(),
        ServiceState(),
    )
    assert bot.sent == [42]
    async with database.session() as session:
        user = await session.scalar(
            select(UserRecord).where(UserRecord.telegram_user_id == 42)
        )
        assert user is not None and user.telegram_panel_message_id == 900
        assert user.telegram_menu_message_id == 55


async def test_start_survives_anchor_and_content_delete_failures(
    database: Database,
) -> None:
    await _user(database, panel_id=77, menu_id=55)
    bot = FakeBot()

    async def fail_delete(_chat_id: int, _message_id: int) -> None:
        raise RuntimeError("cannot delete")

    bot.delete_message = fail_delete  # type: ignore[method-assign]
    message = cast(
        Any,
        SimpleNamespace(
            from_user=SimpleNamespace(id=42, full_name="Rose", language_code="ru"),
            chat=SimpleNamespace(id=42),
            message_id=67,
        ),
    )
    await clean_start_handler(
        message,
        CommandObject(prefix="/", command="start", mention=None),
        cast(Bot, cast(Any, bot)),
        database,
        RuntimeSecrets(),
        AppSettings.model_validate({"onboarding": {"invite_only": False}}),
    )
    assert bot.sent == [42, 42]
    async with database.session() as session:
        user = await session.scalar(
            select(UserRecord).where(UserRecord.telegram_user_id == 42)
        )
        assert user is not None and user.telegram_menu_message_id == 900
        assert user.telegram_panel_message_id == 901


async def test_portfolio_inline_tab_edits_the_saved_screen(database: Database) -> None:
    user = await _user(database, panel_id=77)
    async with database.session() as session, session.begin():
        account = ExchangeAccountRecord(
            user_id=user.id,
            exchange="BINANCE",
            permissions_json={},
            key_last4="1234",
        )
        session.add(account)
        await session.flush()
        session.add(
            PortfolioSnapshotRecord(
                exchange_account_id=account.id,
                ts=datetime.now(UTC),
                spot_value=Decimal("10"),
                futures_wallet=Decimal("0"),
                available=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                earn_value=Decimal("0"),
                funding_value=Decimal("0"),
                source_status_json={},
                data_quality="FRESH",
            )
        )
        session.add(
            SpotHoldingRecord(
                exchange_account_id=account.id,
                asset="USDT",
                free=Decimal("10"),
                locked=Decimal("0"),
                value_usdt=Decimal("10"),
            )
        )
    callback = cast(
        Any,
        SimpleNamespace(
            data="screen:portfolio:spot",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(chat=SimpleNamespace(id=42), message_id=77),
            answer=AsyncMock(),
        ),
    )
    bot = FakeBot()
    await clean_screen_callback(
        callback,
        cast(Bot, cast(Any, bot)),
        database,
        AppSettings(),
        RuntimeSecrets(),
        ConnectSessionStore(),
        ServiceState(),
        cast(Any, None),
    )
    assert bot.edits == [77]
    assert bot.sent == []


async def test_manual_refresh_invokes_existing_reconciler(database: Database) -> None:
    user = await _user(database, panel_id=77)
    async with database.session() as session, session.begin():
        account = ExchangeAccountRecord(
            user_id=user.id,
            exchange="BINANCE",
            permissions_json={},
            key_last4="1234",
        )
        session.add(account)
        await session.flush()
        account_id = account.id

    calls: list[object] = []

    class FakeReconciler:
        async def reconcile_now(self, received_account_id: object) -> bool:
            calls.append(received_account_id)
            return True

    await _refresh_portfolio(database, cast(Any, FakeReconciler()), 42)
    assert calls == [account_id]


async def test_signal_delivery_persists_message_and_delete_after(database: Database) -> None:
    user = await _user(database)
    async with database.session() as session, session.begin():
        signal = SignalRecord(symbol="BTCUSDT", direction="LONG", state="WATCH", score=72)
        session.add(signal)
        await session.flush()
        job = DeliveryRecord(
            signal_id=signal.id,
            user_id=user.id,
            chat_id=42,
            stage="WATCH",
            action="SEND",
            status="PROCESSING",
        )
        session.add(job)
        await session.flush()
        job_id = job.id
    before = datetime.now(UTC)
    bot = FakeBot()
    worker = DeliveryWorker(database, cast(Bot, cast(Any, bot)), signal_ttl_hours=47)
    await worker._deliver(job_id)
    async with database.session() as session:
        stored = await session.get(DeliveryRecord, job_id)
        assert stored is not None and stored.message_id == 900
        assert stored.delete_after is not None
        assert stored.delete_after.replace(tzinfo=UTC) >= before + timedelta(hours=46)


async def test_cleanup_only_deletes_due_signal_messages(database: Database) -> None:
    user = await _user(database, panel_id=777)
    now = datetime.now(UTC)
    async with database.session() as session, session.begin():
        account = ExchangeAccountRecord(
            user_id=user.id,
            exchange="BINANCE",
            permissions_json={},
            key_last4="1234",
        )
        session.add(account)
        signals = [
            SignalRecord(symbol="BTCUSDT", direction="LONG", state="WATCH", score=72),
            SignalRecord(symbol="ETHUSDT", direction="SHORT", state="WATCH", score=75),
        ]
        session.add_all(signals)
        await session.flush()
        session.add_all(
            [
                DeliveryRecord(
                    signal_id=signals[0].id,
                    user_id=user.id,
                    chat_id=42,
                    message_id=101,
                    stage="WATCH",
                    action="SEND",
                    status="SENT",
                    delete_after=now - timedelta(seconds=1),
                ),
                DeliveryRecord(
                    signal_id=signals[1].id,
                    user_id=user.id,
                    chat_id=42,
                    message_id=102,
                    stage="WATCH",
                    action="SEND",
                    status="SENT",
                    delete_after=now + timedelta(hours=1),
                ),
            ]
        )
        session.add(
            RiskAlertRecord(
                user_id=user.id,
                exchange_account_id=account.id,
                chat_id=42,
                alert_type="MARGIN_RISK",
                dedupe_key="risk-1",
                message="critical",
                status="SENT",
                active=True,
            )
        )
    bot = FakeBot()
    cleaned = await DeliveryCleanupWorker(
        database,
        cast(Bot, cast(Any, bot)),
    ).cleanup_once(now=now)
    assert cleaned == 1
    assert bot.deleted == [(42, 101)]
    assert (42, 777) not in bot.deleted


async def test_already_deleted_signal_is_terminal_and_history_survives(
    database: Database,
) -> None:
    user = await _user(database)
    now = datetime.now(UTC)
    async with database.session() as session, session.begin():
        signal = SignalRecord(
            symbol="SOLUSDT",
            direction="LONG",
            state="CONFIRMED",
            score=88,
            watch_at=now,
        )
        session.add(signal)
        await session.flush()
        job = DeliveryRecord(
            signal_id=signal.id,
            user_id=user.id,
            chat_id=42,
            message_id=103,
            stage="CONFIRMED",
            action="EDIT",
            status="SENT",
            delete_after=now - timedelta(seconds=1),
        )
        session.add(job)
        await session.flush()
        job_id = job.id
    error = TelegramBadRequest(
        method=DeleteMessage(chat_id=42, message_id=103),
        message="message to delete not found",
    )
    bot = FakeBot()

    async def missing(_chat_id: int, _message_id: int) -> None:
        raise error

    bot.delete_message = missing  # type: ignore[method-assign]
    worker = DeliveryCleanupWorker(database, cast(Bot, cast(Any, bot)))
    assert await worker.cleanup_once(now=now) == 1
    async with database.session() as session:
        stored = await session.get(DeliveryRecord, job_id)
        assert stored is not None and stored.deleted_at is not None
        assert stored.cleanup_error == "already_deleted"
    assert "SOLUSDT" in await _history_text(database)
