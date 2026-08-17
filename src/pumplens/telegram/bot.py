"""Aiogram application lifecycle. / Жизненный цикл приложения aiogram."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from pumplens.config import AppSettings, RuntimeSecrets
from pumplens.personal_monitor.service import PersonalMonitorService
from pumplens.portfolio.service import PortfolioReconciler
from pumplens.service_state import ServiceState
from pumplens.storage.db import Database
from pumplens.telegram.clean_ui import router as clean_ui_router
from pumplens.telegram.handlers import router as legacy_router
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
    portfolio_reconciler: PortfolioReconciler,
    personal_monitor: PersonalMonitorService,
) -> None:
    dispatcher = Dispatcher()
    dispatcher.include_router(clean_ui_router)
    dispatcher.include_router(legacy_router)
    await dispatcher.start_polling(
        bot,
        database=database,
        settings=settings,
        runtime=runtime,
        connect_sessions=connect_sessions,
        service_state=service_state,
        portfolio_reconciler=portfolio_reconciler,
        personal_monitor=personal_monitor,
    )
