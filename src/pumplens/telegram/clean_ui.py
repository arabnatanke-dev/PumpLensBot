"""Single-message Telegram UX. / Telegram UX на одном сообщении."""

from __future__ import annotations

import html
from typing import cast
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pumplens.analytics.early_stats import load_early_statistics
from pumplens.config import AppSettings, RuntimeSecrets
from pumplens.onboarding.invites import InviteError
from pumplens.onboarding.service import OnboardingService
from pumplens.service_state import ServiceState
from pumplens.storage.db import Database
from pumplens.storage.models import (
    ExchangeAccountRecord,
    PortfolioSnapshotRecord,
    PositionRecord,
    SignalRecord,
    UserPreferenceRecord,
    UserRecord,
)
from pumplens.telegram.formatter import format_early_statistics, format_portfolio, format_positions
from pumplens.telegram.keyboards import (
    binance_keyboard,
    consent_keyboard,
    directions_keyboard,
    main_reply_keyboard,
    panel_back_keyboard,
    panel_binance_keyboard,
    panel_settings_keyboard,
    profile_keyboard,
    welcome_keyboard,
)
from pumplens.telegram.panel import delete_command_best_effort, show_or_edit_panel
from pumplens.webapp.sessions import ConnectSessionStore

router = Router(name="pumplens-clean-ui")
onboarding = OnboardingService()


@router.message(CommandStart())
async def clean_start_handler(
    message: Message,
    command: CommandObject,
    bot: Bot,
    database: Database,
    runtime: RuntimeSecrets,
    settings: AppSettings,
) -> None:
    telegram_user = message.from_user
    if telegram_user is None:
        return
    try:
        async with database.session() as session, session.begin():
            user = await onboarding.start(
                session,
                telegram_user_id=telegram_user.id,
                chat_id=message.chat.id,
                display_name=telegram_user.full_name,
                language=telegram_user.language_code or "ru",
                invite_code=command.args,
                invite_only=settings.onboarding.invite_only,
                bypass_invite=telegram_user.id in runtime.admin_ids,
            )
            complete = user.onboarding_state == "COMPLETE"
    except InviteError:
        await message.answer("Доступ пока только по персональной invite-ссылке.")
        return
    await delete_command_best_effort(bot, message)
    if complete:
        await bot.send_message(
            message.chat.id,
            "<b>🤖 PumpLens</b>\nВыберите раздел в постоянном меню.",
            reply_markup=main_reply_keyboard(),
        )
        return
    text = (
        "👋 PumpLens замечает необычное движение и объясняет причины. "
        "Он не открывает сделки."
    )
    await show_or_edit_panel(
        bot,
        database,
        telegram_user_id=telegram_user.id,
        chat_id=message.chat.id,
        text=text,
        reply_markup=welcome_keyboard(),
    )


@router.callback_query(F.data == "onboarding:about")
async def clean_about_callback(callback: CallbackQuery, bot: Bot, database: Database) -> None:
    await callback.answer()
    await _edit_callback(
        callback,
        bot,
        database,
        "Бот сканирует USDⓈ-M фьючерсы, показывает WATCH/CONFIRMED и добавляет "
        "контекст read-only портфеля.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="onboarding:back")]
            ]
        ),
    )


@router.callback_query(F.data == "onboarding:back")
async def clean_onboarding_back(
    callback: CallbackQuery,
    bot: Bot,
    database: Database,
) -> None:
    await callback.answer()
    await _edit_callback(
        callback,
        bot,
        database,
        "👋 PumpLens замечает необычное движение и объясняет причины. Он не открывает сделки.",
        welcome_keyboard(),
    )


@router.callback_query(F.data == "onboarding:begin")
async def clean_begin_callback(callback: CallbackQuery, bot: Bot, database: Database) -> None:
    await callback.answer()
    async with database.session() as session, session.begin():
        user = await _user_by_telegram(session, callback.from_user.id)
        if user is None:
            return
        user.onboarding_state = "CONSENT"
    await _edit_callback(
        callback,
        bot,
        database,
        "Безопасность: бот не торгует; подключается только read-only ключ; данные "
        "шифруются. Принимаете условия и privacy policy v1.0?",
        consent_keyboard(),
    )


@router.callback_query(F.data == "onboarding:consent")
async def clean_consent_callback(callback: CallbackQuery, bot: Bot, database: Database) -> None:
    await callback.answer()
    telegram_user = callback.from_user
    async with database.session() as session, session.begin():
        user = await _user_by_telegram(session, telegram_user.id)
        if user is None:
            return
        await onboarding.accept_consent(
            session,
            user,
            privacy_version="1.0",
            terms_version="1.0",
            telegram_metadata={"id": telegram_user.id, "language": telegram_user.language_code},
        )
    await _edit_callback(callback, bot, database, "Выберите профиль сигналов:", profile_keyboard())


@router.callback_query(F.data.startswith("profile:"))
async def clean_profile_callback(callback: CallbackQuery, bot: Bot, database: Database) -> None:
    await callback.answer()
    profile = (callback.data or "").split(":", 1)[1]
    async with database.session() as session, session.begin():
        user = await _user_by_telegram(session, callback.from_user.id)
        if user is None:
            return
        await onboarding.set_profile(session, user, profile)
    await _edit_callback(
        callback,
        bot,
        database,
        "Какие направления показывать?",
        directions_keyboard(),
    )


@router.callback_query(F.data.startswith("directions:"))
async def clean_directions_callback(
    callback: CallbackQuery,
    bot: Bot,
    database: Database,
    runtime: RuntimeSecrets,
    connect_sessions: ConnectSessionStore,
) -> None:
    await callback.answer()
    choice = (callback.data or "").split(":", 1)[1]
    async with database.session() as session, session.begin():
        user = await _user_by_telegram(session, callback.from_user.id)
        if user is None:
            return
        await onboarding.set_directions(session, user, _directions(choice))
    await _edit_callback(
        callback,
        bot,
        database,
        "Можно подключить read-only Binance через Mini App или сделать это позже.",
        binance_keyboard(_connect_url(runtime, connect_sessions, callback.from_user.id)),
    )


@router.callback_query(F.data == "binance:skip")
async def clean_skip_binance(
    callback: CallbackQuery,
    bot: Bot,
    database: Database,
) -> None:
    await callback.answer()
    async with database.session() as session, session.begin():
        user = await _user_by_telegram(session, callback.from_user.id)
        if user is None:
            return
        onboarding.complete(user)
    if callback.message is None:
        return
    await bot.edit_message_text(
        "✅ Onboarding завершён. Используйте постоянное меню ниже.",
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
    )
    await bot.send_message(
        callback.message.chat.id,
        "<b>🤖 PumpLens</b>\nВыберите раздел.",
        reply_markup=main_reply_keyboard(),
    )


@router.callback_query(F.data.startswith("panel:"))
async def clean_panel_callback(
    callback: CallbackQuery,
    bot: Bot,
    database: Database,
    settings: AppSettings,
    runtime: RuntimeSecrets,
    connect_sessions: ConnectSessionStore,
    service_state: ServiceState,
) -> None:
    await callback.answer()
    data = callback.data or "panel:status"
    if data.startswith("panel:profile:"):
        await _set_profile(database, callback.from_user.id, data.rsplit(":", 1)[1])
        section = "settings"
    elif data.startswith("panel:directions:"):
        await _set_directions(database, callback.from_user.id, data.rsplit(":", 1)[1])
        section = "settings"
    else:
        section = data.split(":", 1)[1]
        if section in {"home", "refresh"}:
            await _show_menu_hint(callback, bot)
            return
    text, keyboard = await panel_content(
        section,
        callback.from_user.id,
        database,
        settings,
        runtime,
        connect_sessions,
        service_state,
    )
    await _edit_inline_callback(callback, bot, text, keyboard)


@router.callback_query(F.data == "menu:back")
async def clean_menu_back(callback: CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    await _show_menu_hint(callback, bot)


async def _show_menu_hint(callback: CallbackQuery, bot: Bot) -> None:
    if callback.message is None:
        return
    await bot.edit_message_text(
        "Главное меню доступно внизу чата.",
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        reply_markup=None,
    )


async def _edit_inline_callback(
    callback: CallbackQuery,
    bot: Bot,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    if callback.message is None:
        return
    await bot.edit_message_text(
        text,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        reply_markup=keyboard,
    )


async def _edit_callback(
    callback: CallbackQuery,
    bot: Bot,
    database: Database,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    if callback.message is None:
        return
    await show_or_edit_panel(
        bot,
        database,
        telegram_user_id=callback.from_user.id,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        text=text,
        reply_markup=keyboard,
    )


@router.message(
    Command("status", "portfolio", "positions", "early_stats", "history", "binance")
)
async def clean_panel_command(
    message: Message,
    bot: Bot,
    database: Database,
    settings: AppSettings,
    runtime: RuntimeSecrets,
    connect_sessions: ConnectSessionStore,
    service_state: ServiceState,
) -> None:
    if message.from_user is None or not message.text:
        return
    command = message.text.split()[0].split("@", 1)[0].lstrip("/")
    section = {"early_stats": "early"}.get(command, command)
    await delete_command_best_effort(bot, message)
    text, keyboard = await panel_content(
        section,
        message.from_user.id,
        database,
        settings,
        runtime,
        connect_sessions,
        service_state,
    )
    await bot.send_message(
        message.chat.id,
        text,
        reply_markup=keyboard,
    )


REPLY_MENU_SECTIONS = {
    "💼 Портфель": "portfolio",
    "📈 Сигналы": "history",
    "📊 Позиции": "positions",
    "🧪 EARLY": "early",
    "⚙️ Настройки": "settings",
    "🔗 Binance": "binance",
}


@router.message(F.text.in_(set(REPLY_MENU_SECTIONS)))
async def clean_reply_menu_handler(
    message: Message,
    bot: Bot,
    database: Database,
    settings: AppSettings,
    runtime: RuntimeSecrets,
    connect_sessions: ConnectSessionStore,
    service_state: ServiceState,
) -> None:
    if message.from_user is None or message.text is None:
        return
    await delete_command_best_effort(bot, message)
    text, keyboard = await panel_content(
        REPLY_MENU_SECTIONS[message.text],
        message.from_user.id,
        database,
        settings,
        runtime,
        connect_sessions,
        service_state,
    )
    await bot.send_message(message.chat.id, text, reply_markup=keyboard)


async def panel_content(
    section: str,
    telegram_user_id: int,
    database: Database,
    settings: AppSettings,
    runtime: RuntimeSecrets,
    connect_sessions: ConnectSessionStore,
    service_state: ServiceState,
) -> tuple[str, InlineKeyboardMarkup]:
    if section == "status":
        status = service_state.status()
        state = "STALE" if status.stale else "OK"
        return (
            f"<b>📡 Статус</b>\nPumpLens: {state}\nUniverse: {status.universe_size}\n"
            f"Stage B candidates: {status.candidates_count}",
            panel_back_keyboard(),
        )
    if section in {"portfolio", "positions"}:
        data = await _portfolio_data(telegram_user_id, database)
        if data is None:
            return (
                "Binance ещё не подключён или портфель не синхронизирован.",
                panel_back_keyboard(),
            )
        snapshot, positions = data
        formatter = format_portfolio if section == "portfolio" else None
        text = formatter(snapshot, positions) if formatter else format_positions(positions)
        return text, panel_back_keyboard()
    if section == "early":
        return (
            format_early_statistics(await load_early_statistics(database)),
            panel_back_keyboard(),
        )
    if section == "history":
        return await _history_text(database), panel_back_keyboard()
    if section == "settings":
        async with database.session() as session:
            user = await _user_by_telegram(session, telegram_user_id)
            preference = (
                await session.scalar(
                    select(UserPreferenceRecord).where(
                        UserPreferenceRecord.user_id == user.id
                    )
                )
                if user
                else None
            )
            account = await _account_for_user(session, user.id) if user else None
        profile = preference.profile if preference else "balanced"
        directions = list(preference.directions) if preference else ["LONG", "SHORT"]
        connected = account is not None and account.status != "DISCONNECTED"
        account_line = (
            f"Binance: {html.escape(account.status)}" if account else "Binance: не подключён"
        )
        return (
            f"<b>⚙️ Настройки</b>\nПрофиль: {profile.upper()}\n"
            f"Направления: {' + '.join(directions)}\n{account_line}",
            panel_settings_keyboard(
                profile=profile,
                directions=directions,
                connected=connected,
                connect_url=_connect_url(runtime, connect_sessions, telegram_user_id),
            ),
        )
    if section == "binance":
        async with database.session() as session:
            user = await _user_by_telegram(session, telegram_user_id)
            account = await _account_for_user(session, user.id) if user else None
        connected = account is not None and account.status != "DISCONNECTED"
        if connected and account is not None:
            sync = (
                account.last_sync_at.strftime("%d.%m %H:%M UTC")
                if account.last_sync_at
                else "ожидается"
            )
            text = (
                f"<b>🔗 Binance</b>\nСтатус: {html.escape(account.status)}\n"
                f"Ключ: ****{html.escape(account.key_last4)}\nСинхронизация: {sync}"
            )
        else:
            text = "<b>🔗 Binance</b>\nRead-only аккаунт не подключён."
        return (
            text,
            panel_binance_keyboard(
                connected=connected,
                connect_url=_connect_url(runtime, connect_sessions, telegram_user_id),
            ),
        )
    return "Раздел не найден.", panel_back_keyboard()


async def _history_text(database: Database) -> str:
    async with database.session() as session:
        signals = list(
            await session.scalars(
                select(SignalRecord)
                .where(SignalRecord.watch_at.is_not(None))
                .order_by(SignalRecord.watch_at.desc())
                .limit(10)
            )
        )
    if not signals:
        return "<b>📈 История</b>\nИстория сигналов пока пуста."
    lines = ["<b>📈 Последние сигналы</b>"]
    for signal in signals:
        timestamp = signal.watch_at.strftime("%d.%m %H:%M") if signal.watch_at else "—"
        lines.append(
            f"• <b>{html.escape(signal.symbol)}</b> · {html.escape(signal.state)} · "
            f"{signal.direction} · {float(signal.score):.0f} · {timestamp} UTC"
        )
    return "\n".join(lines)


async def _set_profile(database: Database, telegram_user_id: int, profile: str) -> None:
    if profile not in {"safe", "balanced", "wild"}:
        return
    async with database.session() as session, session.begin():
        preference = await _preference_for_telegram(session, telegram_user_id)
        if preference is not None:
            preference.profile = profile


async def _set_directions(database: Database, telegram_user_id: int, choice: str) -> None:
    async with database.session() as session, session.begin():
        preference = await _preference_for_telegram(session, telegram_user_id)
        if preference is not None:
            preference.directions = _directions(choice)


async def _preference_for_telegram(
    session: AsyncSession,
    telegram_user_id: int,
) -> UserPreferenceRecord | None:
    user = await _user_by_telegram(session, telegram_user_id)
    if user is None:
        return None
    return cast(
        UserPreferenceRecord | None,
        await session.scalar(
            select(UserPreferenceRecord).where(UserPreferenceRecord.user_id == user.id)
        ),
    )


async def _portfolio_data(
    telegram_user_id: int,
    database: Database,
) -> tuple[PortfolioSnapshotRecord, list[PositionRecord]] | None:
    async with database.session() as session:
        user = await _user_by_telegram(session, telegram_user_id)
        if user is None:
            return None
        account = await _account_for_user(session, user.id)
        if account is None or account.status == "DISCONNECTED":
            return None
        snapshot = await session.scalar(
            select(PortfolioSnapshotRecord)
            .where(PortfolioSnapshotRecord.exchange_account_id == account.id)
            .order_by(PortfolioSnapshotRecord.ts.desc())
            .limit(1)
        )
        if snapshot is None:
            return None
        positions = list(
            await session.scalars(
                select(PositionRecord).where(PositionRecord.exchange_account_id == account.id)
            )
        )
        return snapshot, positions


async def _user_by_telegram(
    session: AsyncSession,
    telegram_user_id: int,
) -> UserRecord | None:
    return cast(
        UserRecord | None,
        await session.scalar(
            select(UserRecord).where(UserRecord.telegram_user_id == telegram_user_id)
        ),
    )


async def _account_for_user(
    session: AsyncSession,
    user_id: object,
) -> ExchangeAccountRecord | None:
    return cast(
        ExchangeAccountRecord | None,
        await session.scalar(
            select(ExchangeAccountRecord).where(
                ExchangeAccountRecord.user_id == user_id,
                ExchangeAccountRecord.exchange == "BINANCE",
            )
        ),
    )


def _directions(choice: str) -> list[str]:
    return {"both": ["LONG", "SHORT"], "long": ["LONG"], "short": ["SHORT"]}.get(
        choice,
        ["LONG", "SHORT"],
    )


def _connect_url(
    runtime: RuntimeSecrets,
    connect_sessions: ConnectSessionStore,
    telegram_user_id: int,
) -> str | None:
    if not runtime.public_base_url:
        return None
    tokens = connect_sessions.create(telegram_user_id)
    return (
        f"{runtime.public_base_url.rstrip('/')}/connect"
        f"?session={quote(tokens.session_token)}&csrf={quote(tokens.csrf_token)}"
    )
