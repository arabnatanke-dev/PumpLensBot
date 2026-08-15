"""Aiogram application lifecycle. / Жизненный цикл приложения aiogram."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from pumplens.config import AppSettings, RuntimeSecrets
from pumplens.service_state import ServiceState
from pumplens.storage.db import Database
from pumplens.telegram.handlers import router
from pumplens.webapp.sessions import ConnectSessionStore


def create_bot(token: str) -> Bot:
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def run_bot(
    bot: Bot,
    *,
    database: Database,
    settings: AppSettings,
    runtime: RuntimeSecrets,
    connect_sessions: ConnectSessionStore,
    service_state: ServiceState,
) -> None:
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await dispatcher.start_polling(
        bot,
        database=database,
        settings=settings,
        runtime=runtime,
        connect_sessions=connect_sessions,
        service_state=service_state,
    )
