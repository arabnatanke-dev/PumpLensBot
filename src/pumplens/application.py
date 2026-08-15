"""Full service composition root. / Корень сборки полного сервиса."""

from __future__ import annotations

import asyncio

from pumplens.cli.scanner import run_scanner
from pumplens.config import AppSettings, RuntimeSecrets
from pumplens.portfolio.service import PortfolioReconciler, PortfolioService
from pumplens.security.credential_vault import CredentialVault
from pumplens.service_state import ServiceState
from pumplens.storage.db import Database
from pumplens.telegram.bot import create_bot, run_bot
from pumplens.telegram.notifications import DeliveryWorker, TransitionFanout
from pumplens.webapp.app import create_web_app
from pumplens.webapp.server import run_web_server
from pumplens.webapp.sessions import ConnectSessionStore


async def run_full_service(settings: AppSettings, runtime: RuntimeSecrets) -> None:
    token = _required(runtime.telegram_bot_token, "TELEGRAM_BOT_TOKEN")
    database_url = _required(runtime.database_url, "DATABASE_URL")
    master_key = _required(runtime.credential_master_key, "CREDENTIAL_MASTER_KEY")

    database = Database(database_url)
    connect_sessions = ConnectSessionStore(
        ttl_seconds=settings.onboarding.connect_session_ttl_seconds
    )
    service_state = ServiceState()
    bot = create_bot(token)
    fanout = TransitionFanout(database)
    delivery_worker = DeliveryWorker(database, bot)
    portfolio_reconciler = PortfolioReconciler(
        database,
        PortfolioService(CredentialVault(master_key)),
        interval_seconds=settings.portfolio.rest_reconcile_seconds,
    )
    web_app = create_web_app(
        runtime=runtime,
        database=database,
        connect_sessions=connect_sessions,
        service_state=service_state,
    )
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(
                run_scanner(
                    settings,
                    service_state=service_state,
                    transition_handler=fanout,
                ),
                name="scanner",
            )
            group.create_task(
                run_bot(
                    bot,
                    database=database,
                    settings=settings,
                    runtime=runtime,
                    connect_sessions=connect_sessions,
                    service_state=service_state,
                ),
                name="telegram-bot",
            )
            group.create_task(
                run_web_server(web_app, runtime.health_host, runtime.health_port),
                name="mini-app",
            )
            group.create_task(portfolio_reconciler.run(), name="portfolio-reconciler")
            group.create_task(delivery_worker.run(), name="telegram-delivery")
    finally:
        await bot.session.close()
        await database.dispose()


def _required(value: object, name: str) -> str:
    if value is None or not hasattr(value, "get_secret_value"):
        raise ValueError(f"{name} is required / обязательна")
    secret = value.get_secret_value()
    if not secret:
        raise ValueError(f"{name} is required / обязательна")
    return str(secret)
