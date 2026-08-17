"""Persistent personal monitor runtime. / Runtime персонального мониторинга с БД."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from aiogram import Bot
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pumplens.analytics.buffers import MarketState
from pumplens.binance.public_rest import BinancePublicClient
from pumplens.binance.ws_personal import PersonalMarketWebSocket
from pumplens.config import AppSettings
from pumplens.domain.events import (
    BookTickerEvent,
    KlineEvent,
    MarketEvent,
    MarkPriceEvent,
    TickerEvent,
)
from pumplens.domain.models import FeatureSnapshot, Kline, SymbolSpec
from pumplens.personal_monitor.analyzer import PersonalSymbolAnalyzer, analysis_payload
from pumplens.personal_monitor.domain import (
    EvaluationDecision,
    FullAnalysisResult,
    ScenarioPlan,
    ScenarioStatus,
)
from pumplens.personal_monitor.evaluator import evaluate_scenario
from pumplens.personal_monitor.presentation import format_changed, format_confirmed, format_scenario
from pumplens.storage.db import Database
from pumplens.storage.models import MonitoredScenarioRecord, UserRecord
from pumplens.telegram.keyboards import personal_monitor_result_keyboard

log = structlog.get_logger(__name__)


class UnknownFuturesSymbol(ValueError):
    """The symbol is not a live USD-M perpetual. / Символ не является активным USD-M."""


@dataclass(slots=True)
class SymbolRuntime:
    state: MarketState
    analyzer: PersonalSymbolAnalyzer
    lock: asyncio.Lock
    ready: asyncio.Event
    warmup_error: Exception | None = None


class PersonalMonitorService:
    """Owns only caches and streams needed by active personal monitors."""

    def __init__(
        self,
        database: Database,
        bot: Bot,
        settings: AppSettings,
    ) -> None:
        self._database = database
        self._bot = bot
        self._settings = settings
        self._rest = BinancePublicClient(
            settings.binance.rest_base_url,
            settings.binance.request_timeout_seconds,
        )
        self._runtimes: dict[str, SymbolRuntime] = {}
        self._runtime_lock = asyncio.Lock()
        self._ws = PersonalMarketWebSocket(
            settings.binance.websocket_base_url,
            self._handle_event,
            reconnect_max_seconds=settings.binance.reconnect_max_seconds,
        )
        self._exchange_symbols: dict[str, SymbolSpec] = {}
        self._exchange_symbols_loaded_at = 0.0
        self._candle_tasks: dict[str, asyncio.Task[None]] = {}

    async def run(self) -> None:
        if not self._settings.personal_monitor.enabled:
            await asyncio.Event().wait()
        async with asyncio.TaskGroup() as group:
            group.create_task(self._ws.run(), name="personal-monitor-websocket")
            group.create_task(self._discovery_loop(), name="personal-monitor-discovery")

    async def close(self) -> None:
        await self._rest.aclose()

    async def analyze_for_user(
        self,
        telegram_user_id: int,
        chat_id: int,
        raw_symbol: str,
    ) -> MonitoredScenarioRecord:
        symbol = _normalize_symbol(raw_symbol)
        await self._validate_symbol(symbol)
        runtime = await self._ensure_runtime(symbol)
        async with runtime.lock:
            result = runtime.analyzer.analyze(symbol)
        async with self._database.session() as session, session.begin():
            user = await session.scalar(
                select(UserRecord).where(UserRecord.telegram_user_id == telegram_user_id)
            )
            if user is None:
                raise ValueError("Telegram user is not registered")
            record = self._new_record(user, chat_id, result)
            session.add(record)
            await session.flush()
            return record

    async def activate(
        self,
        telegram_user_id: int,
        scenario_id: uuid.UUID,
    ) -> MonitoredScenarioRecord:
        async with self._database.session() as session, session.begin():
            user = await self._user(session, telegram_user_id)
            scenario = await session.get(MonitoredScenarioRecord, scenario_id)
            if scenario is None or scenario.user_id != user.id:
                raise ValueError("Scenario not found")
            await session.execute(
                update(MonitoredScenarioRecord)
                .where(
                    MonitoredScenarioRecord.user_id == user.id,
                    MonitoredScenarioRecord.is_active.is_(True),
                )
                .values(
                    is_active=False,
                    status=ScenarioStatus.STOPPED.value,
                    stopped_at=datetime.now(UTC),
                )
            )
            scenario.is_active = True
            scenario.status = (
                ScenarioStatus.WAITING.value
                if scenario.direction is not None
                else ScenarioStatus.SEARCHING.value
            )
            scenario.stopped_at = None
            await session.flush()
            symbol = scenario.symbol
        await self._ensure_runtime(symbol)
        await self._sync_subscriptions()
        return scenario

    async def recalculate(
        self,
        telegram_user_id: int,
        scenario_id: uuid.UUID,
    ) -> MonitoredScenarioRecord:
        async with self._database.session() as session:
            user = await self._user(session, telegram_user_id)
            source = await session.get(MonitoredScenarioRecord, scenario_id)
            if source is None or source.user_id != user.id:
                raise ValueError("Scenario not found")
            symbol = source.symbol
            chat_id = source.chat_id
        return await self.analyze_for_user(telegram_user_id, chat_id, symbol)

    async def stop(
        self,
        telegram_user_id: int,
        scenario_id: uuid.UUID,
    ) -> MonitoredScenarioRecord:
        async with self._database.session() as session, session.begin():
            user = await self._user(session, telegram_user_id)
            scenario = await session.get(MonitoredScenarioRecord, scenario_id)
            if scenario is None or scenario.user_id != user.id:
                raise ValueError("Scenario not found")
            scenario.status = ScenarioStatus.STOPPED.value
            scenario.is_active = False
            scenario.stopped_at = datetime.now(UTC)
            await session.flush()
        await self._sync_subscriptions()
        return scenario

    async def current(self, telegram_user_id: int) -> MonitoredScenarioRecord | None:
        async with self._database.session() as session:
            user = await session.scalar(
                select(UserRecord).where(UserRecord.telegram_user_id == telegram_user_id)
            )
            if user is None:
                return None
            result = await session.scalar(
                select(MonitoredScenarioRecord)
                .where(MonitoredScenarioRecord.user_id == user.id)
                .order_by(
                    MonitoredScenarioRecord.is_active.desc(),
                    MonitoredScenarioRecord.created_at.desc(),
                )
                .limit(1)
            )
            if result is not None:
                runtime = self._runtimes.get(result.symbol)
                buffer = runtime.state.get(result.symbol) if runtime is not None else None
                if buffer is not None:
                    if buffer.current_kline is not None:
                        result.latest_price = Decimal(str(buffer.current_kline.close))
                    elif buffer.ticker is not None:
                        result.latest_price = Decimal(str(buffer.ticker.last_price))
            return result

    async def _discovery_loop(self) -> None:
        while True:
            await self._sync_subscriptions()
            await asyncio.sleep(self._settings.personal_monitor.discovery_seconds)

    async def _sync_subscriptions(self) -> None:
        async with self._database.session() as session:
            symbols = set(
                await session.scalars(
                    select(MonitoredScenarioRecord.symbol)
                    .where(MonitoredScenarioRecord.is_active.is_(True))
                    .distinct()
                )
            )
        symbols = set(sorted(symbols)[: self._settings.personal_monitor.max_active_symbols])
        for symbol in symbols:
            try:
                await self._ensure_runtime(symbol)
            except Exception as exc:
                log.warning(
                    "personal_monitor_warmup_failed",
                    symbol=symbol,
                    error=type(exc).__name__,
                )
        async with self._runtime_lock:
            for symbol in set(self._runtimes) - symbols:
                self._runtimes.pop(symbol, None)
        await self._ws.set_symbols(symbols)

    async def _ensure_runtime(self, symbol: str) -> SymbolRuntime:
        creator = False
        async with self._runtime_lock:
            existing = self._runtimes.get(symbol)
            if existing is None:
                if len(self._runtimes) >= self._settings.personal_monitor.max_active_symbols:
                    raise RuntimeError("personal monitor symbol limit reached")
                state = MarketState([symbol])
                runtime = SymbolRuntime(
                    state=state,
                    analyzer=PersonalSymbolAnalyzer(state, self._settings),
                    lock=asyncio.Lock(),
                    ready=asyncio.Event(),
                )
                self._runtimes[symbol] = runtime
                creator = True
            else:
                runtime = existing
        if creator:
            try:
                await self._warmup(symbol, runtime.state)
                await self._ws.set_symbols(self._runtimes)
            except Exception as exc:
                runtime.warmup_error = exc
                async with self._runtime_lock:
                    self._runtimes.pop(symbol, None)
            finally:
                runtime.ready.set()
        else:
            await runtime.ready.wait()
        if runtime.warmup_error is not None:
            raise RuntimeError(
                f"personal monitor warmup failed for {symbol}"
            ) from runtime.warmup_error
        return runtime

    async def _warmup(self, symbol: str, state: MarketState) -> None:
        klines, ticker, book, mark = await asyncio.gather(
            self._rest.klines(symbol, limit=self._settings.personal_monitor.warmup_klines),
            self._rest.ticker_24h(symbol),
            self._rest.book_ticker(symbol),
            self._rest.mark_price(symbol),
        )
        state.seed(symbol, klines)
        if klines:
            await state.handle(KlineEvent(klines[-1]))
        await state.handle(TickerEvent(ticker))
        await state.handle(BookTickerEvent(book))
        await state.handle(MarkPriceEvent(mark))
        try:
            state.record_open_interest(await self._rest.open_interest(symbol))
        except Exception as exc:
            # OI is optional; missing data must not stop the monitor.
            # OI опционален; его отсутствие не останавливает монитор.
            log.warning(
                "personal_monitor_oi_warmup_failed",
                symbol=symbol,
                error=type(exc).__name__,
            )

    async def _handle_event(self, event: MarketEvent) -> None:
        symbol = event.value.symbol
        runtime = self._runtimes.get(symbol)
        if runtime is None:
            return
        await runtime.state.handle(event)
        if not isinstance(event, KlineEvent) or not event.value.closed:
            return
        current_task = self._candle_tasks.get(symbol)
        if current_task is not None and not current_task.done():
            return
        task = asyncio.create_task(self._process_closed_candle(symbol, event.value))
        self._candle_tasks[symbol] = task

        def done_callback(done: asyncio.Task[None]) -> None:
            self._candle_done(symbol, done)

        task.add_done_callback(done_callback)

    def _candle_done(self, symbol: str, task: asyncio.Task[None]) -> None:
        if self._candle_tasks.get(symbol) is task:
            self._candle_tasks.pop(symbol, None)
        with contextlib.suppress(asyncio.CancelledError):
            error = task.exception()
            if error is not None:
                log.warning(
                    "personal_monitor_candle_failed",
                    symbol=symbol,
                    error=type(error).__name__,
                )

    async def _process_closed_candle(self, symbol: str, candle: Kline) -> None:
        runtime = self._runtimes.get(symbol)
        if runtime is None or not await self._has_pending_candle(symbol, candle.open_time_ms):
            return
        with contextlib.suppress(Exception):
            runtime.state.record_open_interest(await self._rest.open_interest(symbol))
        async with runtime.lock:
            result = runtime.analyzer.analyze(symbol)
        notifications: list[tuple[int, str, MonitoredScenarioRecord]] = []
        async with self._database.session() as session, session.begin():
            scenarios = list(
                await session.scalars(
                    select(MonitoredScenarioRecord).where(
                        MonitoredScenarioRecord.symbol == symbol,
                        MonitoredScenarioRecord.is_active.is_(True),
                    )
                )
            )
            for scenario in scenarios:
                if scenario.last_evaluated_candle_open_time == candle.open_time_ms:
                    continue
                scenario.last_evaluated_candle_open_time = candle.open_time_ms
                scenario.latest_price = Decimal(str(candle.close))
                if scenario.status == ScenarioStatus.SEARCHING.value:
                    self._apply_result(scenario, result)
                    if result.plan is not None:
                        scenario.status = ScenarioStatus.SCENARIO_FOUND.value
                        scenario.is_active = False
                        notifications.append(
                            (scenario.chat_id, format_scenario(scenario), scenario)
                        )
                    continue
                if scenario.status != ScenarioStatus.WAITING.value:
                    continue
                snapshot = (
                    result.long_snapshot
                    if scenario.direction == "LONG"
                    else result.short_snapshot
                )
                _attach_selected_snapshot(scenario, snapshot)
                decision, reason = evaluate_scenario(
                    scenario,
                    candle,
                    snapshot,
                    self._settings.stage_c,
                )
                scenario.reason_code = reason
                if decision is EvaluationDecision.CONFIRMED:
                    scenario.status = ScenarioStatus.CONFIRMED.value
                    scenario.is_active = False
                    scenario.confirmed_at = datetime.now(UTC)
                    notifications.append((scenario.chat_id, format_confirmed(scenario), scenario))
                elif decision is EvaluationDecision.INVALIDATED:
                    scenario.status = ScenarioStatus.INVALIDATED.value
                    scenario.is_active = False
                    replacement = self._new_record_from_values(
                        scenario.user_id,
                        scenario.chat_id,
                        result,
                    )
                    session.add(replacement)
                    await session.flush()
                    notifications.append(
                        (scenario.chat_id, format_changed(scenario, replacement), replacement)
                    )
        for chat_id, text, record in notifications:
            try:
                await self._bot.send_message(
                    chat_id,
                    text,
                    reply_markup=personal_monitor_result_keyboard(record),
                )
            except Exception as exc:
                log.warning(
                    "personal_monitor_notification_failed",
                    scenario_id=str(record.id),
                    error=type(exc).__name__,
                )
        if notifications:
            await self._sync_subscriptions()

    async def _has_pending_candle(self, symbol: str, open_time_ms: int) -> bool:
        """Reject duplicate candles before OI or analysis work. / Отсекает дубли до расчётов."""

        async with self._database.session() as session:
            scenario_id = await session.scalar(
                select(MonitoredScenarioRecord.id)
                .where(
                    MonitoredScenarioRecord.symbol == symbol,
                    MonitoredScenarioRecord.is_active.is_(True),
                    or_(
                        MonitoredScenarioRecord.last_evaluated_candle_open_time.is_(None),
                        MonitoredScenarioRecord.last_evaluated_candle_open_time
                        != open_time_ms,
                    ),
                )
                .limit(1)
            )
            return scenario_id is not None

    async def _validate_symbol(self, symbol: str) -> None:
        loop = asyncio.get_running_loop()
        if (
            not self._exchange_symbols
            or loop.time() - self._exchange_symbols_loaded_at
            >= self._settings.personal_monitor.exchange_info_ttl_seconds
        ):
            specs = await self._rest.exchange_info()
            self._exchange_symbols = {
                item.symbol: item
                for item in specs
                if item.status == "TRADING"
                and item.contract_type == "PERPETUAL"
                and item.quote_asset == "USDT"
            }
            self._exchange_symbols_loaded_at = loop.time()
        if symbol not in self._exchange_symbols:
            raise UnknownFuturesSymbol(symbol)

    @staticmethod
    async def _user(session: AsyncSession, telegram_user_id: int) -> UserRecord:
        user = await session.scalar(
            select(UserRecord).where(UserRecord.telegram_user_id == telegram_user_id)
        )
        if user is None:
            raise ValueError("Telegram user is not registered")
        return user

    def _new_record(
        self,
        user: UserRecord,
        chat_id: int,
        result: FullAnalysisResult,
    ) -> MonitoredScenarioRecord:
        return self._new_record_from_values(user.id, chat_id, result)

    def _new_record_from_values(
        self,
        user_id: uuid.UUID,
        chat_id: int,
        result: FullAnalysisResult,
    ) -> MonitoredScenarioRecord:
        record = MonitoredScenarioRecord(
            user_id=user_id,
            chat_id=chat_id,
            symbol=result.symbol,
            status=(
                ScenarioStatus.SCENARIO_FOUND.value
                if result.plan is not None
                else ScenarioStatus.SEARCHING.value
            ),
            latest_price=Decimal(str(result.price)),
            long_score=result.long_score,
            short_score=result.short_score,
            analysis_json=analysis_payload(result),
            is_active=False,
        )
        self._apply_plan(record, result.plan)
        return record

    def _apply_result(
        self,
        record: MonitoredScenarioRecord,
        result: FullAnalysisResult,
    ) -> None:
        record.latest_price = Decimal(str(result.price))
        record.long_score = result.long_score
        record.short_score = result.short_score
        record.analysis_json = analysis_payload(result)
        self._apply_plan(record, result.plan)

    @staticmethod
    def _apply_plan(
        record: MonitoredScenarioRecord,
        plan: ScenarioPlan | None,
    ) -> None:
        if plan is None:
            record.direction = None
            record.setup_type = None
            return
        record.direction = plan.direction
        record.setup_type = plan.setup_type.value
        record.trigger_level = Decimal(str(plan.trigger_level))
        record.retest_zone_low = (
            Decimal(str(plan.retest_zone.low)) if plan.retest_zone is not None else None
        )
        record.retest_zone_high = (
            Decimal(str(plan.retest_zone.high)) if plan.retest_zone is not None else None
        )
        record.invalidation_level = Decimal(str(plan.invalidation_level))
        record.target1 = Decimal(str(plan.target1))
        record.require_volume_confirmation = True
        record.require_pressure_confirmation = True
        record.require_oi_confirmation = plan.require_oi_confirmation
        record.breakout_observed = plan.breakout_observed
        record.retest_observed = plan.analysis.retest_started


def _normalize_symbol(raw: str) -> str:
    symbol = "".join(raw.upper().split())
    if not symbol or len(symbol) > 32 or not symbol.isalnum():
        raise UnknownFuturesSymbol(raw)
    return symbol


def _attach_selected_snapshot(
    scenario: MonitoredScenarioRecord,
    snapshot: FeatureSnapshot,
) -> None:
    payload = dict(scenario.analysis_json)
    directional_pressure = (
        snapshot.buy_pressure
        if snapshot.direction.value == "LONG"
        else 1.0 - snapshot.buy_pressure
    )
    payload["selected_snapshot"] = {
        "volume_ratio_1m": snapshot.volume_ratio_1m,
        "trade_rate_ratio": snapshot.trade_rate_ratio,
        "directional_pressure": directional_pressure,
        "oi_delta_pct": snapshot.oi_delta_pct,
    }
    scenario.analysis_json = payload
