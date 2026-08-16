"""Persistent Telegram panel primitives. / Примитивы постоянной Telegram-панели."""

from __future__ import annotations

from contextlib import suppress

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.types import InlineKeyboardMarkup, Message, ReplyKeyboardMarkup
from sqlalchemy import select

from pumplens.storage.db import Database
from pumplens.storage.models import UserRecord

MENU_ANCHOR_TEXT = "<b>⌨️ Меню PumpLens</b>\nВыберите раздел ниже."


async def show_or_edit_panel(
    bot: Bot,
    database: Database,
    *,
    telegram_user_id: int,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None,
    message_id: int | None = None,
    force_send: bool = False,
) -> int:
    """Edit the persistent panel or recreate it safely. / Редактирует или создаёт панель."""

    async with database.session() as session:
        user = await session.scalar(
            select(UserRecord).where(UserRecord.telegram_user_id == telegram_user_id)
        )
        stored_message_id = user.telegram_panel_message_id if user is not None else None
    target_id = message_id or stored_message_id
    if force_send:
        if target_id is not None:
            # Moving the screen is still useful when Telegram cannot delete the old one.
            # Перенос карточки полезен, даже если Telegram не удалил старую.
            with suppress(Exception):
                await bot.delete_message(chat_id, target_id)
        panel = await bot.send_message(chat_id, text, reply_markup=reply_markup)
        await _save_panel_id(database, telegram_user_id, panel.message_id)
        return panel.message_id
    if target_id is not None:
        try:
            # Telegram accepts only inline markup while editing. A persistent reply
            # keyboard is attached when the screen is first created. / Telegram
            # принимает при edit только inline markup; reply menu крепится при создании.
            edit_markup = (
                None if isinstance(reply_markup, ReplyKeyboardMarkup) else reply_markup
            )
            await bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=target_id,
                reply_markup=edit_markup,
            )
            await _save_panel_id(database, telegram_user_id, target_id)
            return target_id
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                await _save_panel_id(database, telegram_user_id, target_id)
                return target_id
        except TelegramNotFound:
            pass
    panel = await bot.send_message(chat_id, text, reply_markup=reply_markup)
    await _save_panel_id(database, telegram_user_id, panel.message_id)
    return panel.message_id


async def delete_command_best_effort(bot: Bot, message: Message) -> None:
    """Keep private chats clean without breaking commands. / Чистит команду без ошибок UX."""

    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        # Deletion is cosmetic and must never block the requested section.
        # Удаление косметическое и не должно блокировать нужный раздел.
        return


async def replace_menu_anchor(
    bot: Bot,
    database: Database,
    *,
    telegram_user_id: int,
    chat_id: int,
    reply_markup: ReplyKeyboardMarkup,
) -> int:
    """Create the reply-keyboard anchor without touching content. / Создаёт anchor меню."""

    async with database.session() as session:
        user = await session.scalar(
            select(UserRecord).where(UserRecord.telegram_user_id == telegram_user_id)
        )
        old_message_id = user.telegram_menu_message_id if user is not None else None

    anchor = await bot.send_message(chat_id, MENU_ANCHOR_TEXT, reply_markup=reply_markup)
    await _save_menu_id(database, telegram_user_id, anchor.message_id)
    if old_message_id is not None and old_message_id != anchor.message_id:
        # The new keyboard is active before cleanup, so deletion failure is harmless.
        # Новая клавиатура уже активна, поэтому ошибка удаления безопасна.
        with suppress(Exception):
            await bot.delete_message(chat_id, old_message_id)
    return anchor.message_id


async def _save_panel_id(
    database: Database,
    telegram_user_id: int,
    message_id: int,
) -> None:
    async with database.session() as session, session.begin():
        user = await session.scalar(
            select(UserRecord).where(UserRecord.telegram_user_id == telegram_user_id)
        )
        if user is not None:
            user.telegram_panel_message_id = message_id


async def _save_menu_id(
    database: Database,
    telegram_user_id: int,
    message_id: int,
) -> None:
    async with database.session() as session, session.begin():
        user = await session.scalar(
            select(UserRecord).where(UserRecord.telegram_user_id == telegram_user_id)
        )
        if user is not None:
            user.telegram_menu_message_id = message_id
