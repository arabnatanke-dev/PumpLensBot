"""Telegram commands and onboarding callbacks. / Команды и callback регистрации."""

from __future__ import annotations

from typing import cast
from urllib.parse import quote

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pumplens.config import AppSettings, RuntimeSecrets
from pumplens.onboarding.invites import InviteError
from pumplens.onboarding.service import OnboardingService
from pumplens.service_state import ServiceState
from pumplens.storage.db import Database
from pumplens.storage.models import (
    ExchangeAccountRecord,
    PortfolioSnapshotRecord,
    PositionRecord,
    UserRecord,
)
from pumplens.telegram.formatter import format_portfolio, format_positions, format_top
from pumplens.telegram.keyboards import (
    binance_keyboard,
    consent_keyboard,
    directions_keyboard,
    profile_keyboard,
    welcome_keyboard,
)
from pumplens.webapp.sessions import ConnectSessionStore

router = Router(name="pumplens")
onboarding = OnboardingService()


@router.message(CommandStart())
async def start_handler(
    message: Message,
    command: CommandObject,
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
            state = user.onboarding_state
    except InviteError:
        await message.answer("Доступ пока только по персональной invite-ссылке.")
        return
    if state == "COMPLETE":
        await message.answer("PumpLens активен. Используйте /top, /status или /help.")
    else:
        await message.answer(
            "👋 PumpLens замечает необычное движение и объясняет причины. Он не открывает сделки.",
            reply_markup=welcome_keyboard(),
        )


@router.callback_query(F.data == "onboarding:about")
async def about_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Бот сканирует USDⓈ-M фьючерсы, показывает WATCH/CONFIRMED "
            "и может добавить контекст вашего read-only портфеля."
        )


@router.callback_query(F.data == "onboarding:begin")
async def begin_callback(callback: CallbackQuery, database: Database) -> None:
    user = await _load_user(callback, database)
    if user is None:
        return
    async with database.session() as session, session.begin():
        stored = await session.get(UserRecord, user.id)
        if stored:
            stored.onboarding_state = "CONSENT"
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Безопасность: бот не торгует; подключается только read-only ключ; "
            "данные шифруются. Принимаете условия и privacy policy v1.0?",
            reply_markup=consent_keyboard(),
        )


@router.callback_query(F.data == "onboarding:consent")
async def consent_callback(callback: CallbackQuery, database: Database) -> None:
    telegram_user = callback.from_user
    async with database.session() as session, session.begin():
        user = await session.scalar(
            select(UserRecord).where(UserRecord.telegram_user_id == telegram_user.id)
        )
        if user is None:
            await callback.answer("Сначала откройте invite-ссылку.", show_alert=True)
            return
        await onboarding.accept_consent(
            session,
            user,
            privacy_version="1.0",
            terms_version="1.0",
            telegram_metadata={"id": telegram_user.id, "language": telegram_user.language_code},
        )
    await callback.answer()
    if callback.message:
        await callback.message.answer("Выберите профиль сигналов:", reply_markup=profile_keyboard())


@router.callback_query(F.data.startswith("profile:"))
async def profile_callback(callback: CallbackQuery, database: Database) -> None:
    profile = (callback.data or "").split(":", 1)[1]
    async with database.session() as session, session.begin():
        user = await _user_by_telegram(session, callback.from_user.id)
        if user is None:
            return
        await onboarding.set_profile(session, user, profile)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Какие направления показывать?",
            reply_markup=directions_keyboard(),
        )


@router.callback_query(F.data.startswith("directions:"))
async def directions_callback(
    callback: CallbackQuery,
    database: Database,
    runtime: RuntimeSecrets,
    connect_sessions: ConnectSessionStore,
) -> None:
    choice = (callback.data or "").split(":", 1)[1]
    directions = {"both": ["LONG", "SHORT"], "long": ["LONG"], "short": ["SHORT"]}[choice]
    async with database.session() as session, session.begin():
        user = await _user_by_telegram(session, callback.from_user.id)
        if user is None:
            return
        await onboarding.set_directions(session, user, directions)
    connect_url = None
    if runtime.public_base_url:
        tokens = connect_sessions.create(callback.from_user.id)
        connect_url = (
            f"{runtime.public_base_url.rstrip('/')}/connect"
            f"?session={quote(tokens.session_token)}&csrf={quote(tokens.csrf_token)}"
        )
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Можно безопасно подключить read-only Binance через Mini App или сделать это позже.",
            reply_markup=binance_keyboard(connect_url),
        )


@router.callback_query(F.data == "binance:skip")
async def skip_binance_callback(callback: CallbackQuery, database: Database) -> None:
    async with database.session() as session, session.begin():
        user = await _user_by_telegram(session, callback.from_user.id)
        if user is None:
            return
        onboarding.complete(user)
    await callback.answer()
    if callback.message:
        await callback.message.answer("✅ Готово. Команды: /top, /status, /help.")


@router.message(Command("top"))
async def top_handler(message: Message, service_state: ServiceState) -> None:
    await message.answer(format_top(service_state.top(10)))


@router.message(Command("status"))
async def status_handler(message: Message, service_state: ServiceState) -> None:
    status = service_state.status()
    state = "STALE" if status.stale else "OK"
    await message.answer(
        f"PumpLens: {state}\nUniverse: {status.universe_size}\n"
        f"Stage B candidates: {status.candidates_count}"
    )


@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(
        "PumpLens наблюдает рынок и не открывает сделки. Score — сила совпадения "
        "признаков, а не вероятность прибыли. Команды: /top, /status, /start."
    )


@router.message(Command("portfolio"))
async def portfolio_handler(message: Message, database: Database) -> None:
    data = await _portfolio_data(message, database)
    if data is None:
        await message.answer("Binance ещё не подключён или портфель не синхронизирован.")
        return
    snapshot, positions = data
    await message.answer(format_portfolio(snapshot, positions))


@router.message(Command("positions"))
async def positions_handler(message: Message, database: Database) -> None:
    data = await _portfolio_data(message, database)
    if data is None:
        await message.answer("Binance ещё не подключён или портфель не синхронизирован.")
        return
    _, positions = data
    await message.answer(format_positions(positions))


async def _load_user(callback: CallbackQuery, database: Database) -> UserRecord | None:
    async with database.session() as session:
        return await _user_by_telegram(session, callback.from_user.id)


async def _user_by_telegram(
    session: AsyncSession,
    telegram_user_id: int,
) -> UserRecord | None:
    # Typed adapter keeps handler bodies small. / Типизированный адаптер упрощает handlers.
    return cast(
        UserRecord | None,
        await session.scalar(
            select(UserRecord).where(UserRecord.telegram_user_id == telegram_user_id)
        ),
    )


async def _portfolio_data(
    message: Message,
    database: Database,
) -> tuple[PortfolioSnapshotRecord, list[PositionRecord]] | None:
    if message.from_user is None:
        return None
    async with database.session() as session:
        user = await _user_by_telegram(session, message.from_user.id)
        if user is None:
            return None
        account = await session.scalar(
            select(ExchangeAccountRecord).where(
                ExchangeAccountRecord.user_id == user.id,
                ExchangeAccountRecord.exchange == "BINANCE",
            )
        )
        if account is None:
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
