"""Full service composition root. / Корень сборки полного сервиса."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pumplens.cli.scanner import run_scanner
from pumplens.config import AppSettings, RuntimeSecrets
from pumplens.personal_monitor.service import PersonalMonitorService
from pumplens.portfolio.live_marks import LiveMarkPriceStore
from pumplens.portfolio.private_stream import PrivateAccountStreamManager
from pumplens.portfolio.risk import PortfolioRiskMonitor, RiskAlertWorker
from pumplens.portfolio.service import PortfolioReconciler, PortfolioService
from pumplens.replay.evaluator import EarlyOutcomeEvaluator, SignalOutcomeEvaluator
from pumplens.security.credential_vault import CredentialVault
from pumplens.service_state import ServiceState
from pumplens.storage.db import Database
from pumplens.telegram.bot import create_bot, run_bot
from pumplens.telegram.notifications import (
    DeliveryCleanupWorker,
    DeliveryWorker,
    TransitionFanout,
)
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
    fanout = TransitionFanout(database, early_shadow_mode=settings.early.shadow_mode)
    delivery_worker = DeliveryWorker(
        database,
        bot,
        runtime.public_base_url,
        signal_ttl_hours=settings.telegram.signal_ttl_hours,
    )
    delivery_cleanup = DeliveryCleanupWorker(
        database,
        bot,
        scan_seconds=settings.telegram.cleanup_scan_seconds,
    )
    portfolio_service = PortfolioService(CredentialVault(master_key))
    portfolio_reconciler = PortfolioReconciler(
        database,
        portfolio_service,
        interval_seconds=settings.portfolio.rest_reconcile_seconds,
    )
    private_streams = PrivateAccountStreamManager(
        database,
        portfolio_service,
        portfolio_reconciler,
        discovery_seconds=settings.portfolio.account_discovery_seconds,
        reconcile_debounce_seconds=settings.portfolio.event_reconcile_debounce_seconds,
        listen_key_keepalive_seconds=settings.portfolio.listen_key_keepalive_seconds,
        max_accounts=settings.portfolio.max_private_accounts_per_instance,
    )
    live_marks = LiveMarkPriceStore()
    risk_monitor = PortfolioRiskMonitor(
        database,
        interval_seconds=settings.portfolio.risk_scan_seconds,
        stale_after_seconds=settings.portfolio.stale_after_seconds,
        liquidation_warning_pct=settings.portfolio.liquidation_warning_pct,
        pnl_milestones_pct=tuple(float(value) for value in settings.portfolio.pnl_milestones_pct),
        live_marks=live_marks,
        live_mark_stale_seconds=settings.portfolio.live_mark_stale_seconds,
    )
    risk_worker = RiskAlertWorker(database, bot, runtime.public_base_url)
    outcome_evaluator = SignalOutcomeEvaluator(database, settings.binance.rest_base_url)
    early_outcome_evaluator = EarlyOutcomeEvaluator(
        database,
        settings.binance.rest_base_url,
        interval_seconds=settings.early.outcome_poll_seconds,
    )
    personal_monitor = PersonalMonitorService(database, bot, settings)
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
                    record_path=Path(settings.replay.record_path)
                    if settings.replay.enabled
                    else None,
                    mark_price_handler=live_marks.update,
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
                    portfolio_reconciler=portfolio_reconciler,
                    personal_monitor=personal_monitor,
                ),
                name="telegram-bot",
            )
            group.create_task(
                run_web_server(web_app, runtime.health_host, runtime.health_port),
                name="mini-app",
            )
            group.create_task(portfolio_reconciler.run(), name="portfolio-reconciler")
            group.create_task(private_streams.run(), name="private-account-streams")
            group.create_task(risk_monitor.run(), name="portfolio-risk-monitor")
            group.create_task(risk_worker.run(), name="portfolio-risk-delivery")
            group.create_task(outcome_evaluator.run(), name="signal-outcome-evaluator")
            group.create_task(early_outcome_evaluator.run(), name="early-outcome-evaluator")
            group.create_task(delivery_worker.run(), name="telegram-delivery")
            group.create_task(delivery_cleanup.run(), name="telegram-signal-cleanup")
            group.create_task(personal_monitor.run(), name="personal-symbol-monitor")
    finally:
        await personal_monitor.close()
        await bot.session.close()
        await database.dispose()


def _required(value: object, name: str) -> str:
    if value is None or not hasattr(value, "get_secret_value"):
        raise ValueError(f"{name} is required / обязательна")
    secret = value.get_secret_value()
    if not secret:
        raise ValueError(f"{name} is required / обязательна")
    return str(secret)
