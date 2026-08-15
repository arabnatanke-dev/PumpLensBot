"""Persistent Telegram panel primitives. / Примитивы постоянной Telegram-панели."""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy import select

from pumplens.storage.db import Database
from pumplens.storage.models import UserRecord


async def show_or_edit_panel(
    bot: Bot,
    database: Database,
    *,
    telegram_user_id: int,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    message_id: int | None = None,
) -> int:
    """Edit the persistent panel or recreate it safely. / Редактирует или создаёт панель."""

    async with database.session() as session:
        user = await session.scalar(
            select(UserRecord).where(UserRecord.telegram_user_id == telegram_user_id)
        )
        stored_message_id = user.telegram_panel_message_id if user is not None else None
    target_id = message_id or stored_message_id
    if target_id is not None:
        try:
            await bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=target_id,
                reply_markup=reply_markup,
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
