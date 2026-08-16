"""First milestone: live top-10 CLI scanner. / Первый этап: живой CLI top-10."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import structlog

from pumplens.analytics.buffers import MarketState, WarmupLoader
from pumplens.analytics.features import FeatureEngine
from pumplens.analytics.fsm import SignalFSM, SignalTransition
from pumplens.analytics.stage_a import StageAScanner
from pumplens.analytics.stage_b import OpenInterestPoller, StageBManager
from pumplens.analytics.stage_c import DeepEntryValidator
from pumplens.binance.normalizer import BinanceNormalizer
from pumplens.binance.public_rest import BinancePublicClient
from pumplens.binance.universe import UniverseManager
from pumplens.binance.ws_deep import DeepMarketWebSocket
from pumplens.binance.ws_market import MarketWebSocket
from pumplens.config import AppSettings
from pumplens.domain.events import KlineEvent, MarketEvent, MarkPriceEvent
from pumplens.domain.models import Candidate, MarkPrice
from pumplens.replay.recorder import MarketEventRecorder
from pumplens.service_state import ServiceState

log = structlog.get_logger(__name__)


async def run_scanner(
    settings: AppSettings,
    *,
    duration_seconds: float | None = None,
    symbol_limit: int | None = None,
    display_seconds: float = 5.0,
    service_state: ServiceState | None = None,
    transition_handler: Callable[[SignalTransition], Awaitable[None]] | None = None,
    record_path: Path | None = None,
    mark_price_handler: Callable[[MarkPrice], None] | None = None,
) -> None:
    """Build public pipeline and run until cancelled. / Собирает публичный pipeline."""

    async with BinancePublicClient(
        settings.binance.rest_base_url,
        timeout_seconds=settings.binance.request_timeout_seconds,
    ) as client:
        server_time = await client.server_time_ms()
        local_time = int(datetime.now(UTC).timestamp() * 1_000)
        lag_ms = abs(local_time - server_time)
        log.info("binance_clock_checked", lag_ms=lag_ms)
        if lag_ms > 3_000:
            log.warning("local_clock_drift", lag_ms=lag_ms)

        universe = await UniverseManager(
            client,
            min_quote_volume_24h=settings.scanner.min_quote_volume_24h,
        ).load()
        symbols: Sequence[str] = universe.names
        if symbol_limit is not None:
            symbols = symbols[:symbol_limit]
        if not symbols:
            raise RuntimeError("Universe is empty / Список анализируемых символов пуст")

        state = MarketState(symbols)
        await WarmupLoader(
            client,
            state,
            concurrency=settings.binance.warmup_concurrency,
        ).load(symbols, limit=settings.scanner.warmup_klines)

        # Seed 24h liquidity values immediately; WebSocket will refresh them.
        # Сразу записываем 24h-ликвидность; далее её обновит WebSocket.
        for symbol in symbols:
            ticker = universe.tickers.get(symbol)
            buffer = state.get(symbol)
            if ticker is not None and buffer is not None:
                buffer.ticker = ticker

        recorder = (
            MarketEventRecorder(
                record_path,
                queue_size=settings.replay.queue_size,
                batch_size=settings.replay.batch_size,
                flush_interval_seconds=settings.replay.flush_interval_seconds,
                max_file_size_bytes=settings.replay.max_file_size_mb * 1024 * 1024,
                retention_days=settings.replay.retention_days,
                gzip_rotated=settings.replay.gzip_rotated,
                record_book_ticker=settings.replay.record_book_ticker,
            )
            if record_path is not None
            else None
        )
        if recorder is not None:
            await recorder.open()
            for symbol in symbols:
                buffer = state.get(symbol)
                if buffer is None:
                    continue
                for kline in buffer.closed_klines:
                    await recorder.record(KlineEvent(kline))
                if buffer.current_kline is not None:
                    await recorder.record(KlineEvent(buffer.current_kline))

        async def market_handler(event: MarketEvent) -> None:
            await state.handle(event)
            if isinstance(event, MarkPriceEvent) and mark_price_handler is not None:
                mark_price_handler(event.value)
            if recorder is not None:
                await recorder.record(event)

        normalizer = BinanceNormalizer(frozenset(symbols))
        feature_engine = FeatureEngine(
            state,
            settings.binance.stale_after_seconds,
            oi_stale_after_seconds=max(
                settings.binance.stale_after_seconds,
                settings.oi.poll_seconds * 2.5,
            ),
        )
        stage_a = StageAScanner(
            feature_engine,
            settings.scanner,
            settings.late,
            settings.early,
        )
        stage_b = StageBManager(max_candidates=settings.scanner.max_deep_candidates)
        stage_c = DeepEntryValidator(state, settings.stage_c)
        signal_fsm = SignalFSM(
            state,
            settings.scanner,
            settings.late,
            settings.early,
            settings.stage_c,
        )
        market_ws = MarketWebSocket(
            settings.binance.websocket_base_url,
            symbols,
            normalizer,
            market_handler,
            streams_per_connection=settings.binance.websocket_streams_per_connection,
            reconnect_max_seconds=settings.binance.reconnect_max_seconds,
            planned_rotation_seconds=settings.binance.planned_rotation_seconds,
        )
        deep_ws = DeepMarketWebSocket(
            settings.binance.websocket_base_url,
            normalizer,
            market_handler,
            reconnect_max_seconds=settings.binance.reconnect_max_seconds,
        )
        oi_poller = OpenInterestPoller(
            client,
            state,
            stage_b,
            interval_seconds=settings.oi.poll_seconds,
            concurrency=settings.binance.warmup_concurrency,
        )

        async def scan_loop() -> None:
            last_display = 0.0
            loop = asyncio.get_running_loop()
            while True:
                stage_a_started = time.perf_counter()
                candidates = stage_a.scan_once()
                stage_a_ms = (time.perf_counter() - stage_a_started) * 1_000
                if service_state is not None:
                    service_state.update(candidates, len(symbols))
                stage_b_started = time.perf_counter()
                active_symbols = stage_b.update(candidates)
                await deep_ws.set_symbols(active_symbols)
                deep_ranked = stage_b.rescore(
                    candidates,
                    settings.scanner.max_spread_pct,
                )
                stage_b_ms = (time.perf_counter() - stage_b_started) * 1_000
                stage_c_started = time.perf_counter()
                prepared_stage_c = stage_c.prepare(deep_ranked)
                validated = await asyncio.to_thread(
                    stage_c.validate_prepared,
                    prepared_stage_c,
                )
                stage_c_ms = (time.perf_counter() - stage_c_started) * 1_000
                deep_candidates = {
                    candidate.snapshot.symbol: candidate
                    for candidate in validated
                }
                # Keep Stage B scoring for retained rows outside the bounded Stage C shortlist.
                # Сохраняем Stage B score вне ограниченного shortlist Stage C.
                for candidate in deep_ranked:
                    deep_candidates.setdefault(candidate.snapshot.symbol, candidate)
                decision_started = time.perf_counter()
                for candidate in candidates:
                    selected_input = deep_candidates.get(candidate.snapshot.symbol, candidate)
                    transition = signal_fsm.advance(selected_input)
                    if transition is not None and transition_handler is not None:
                        await transition_handler(transition)
                decision_ms = (time.perf_counter() - decision_started) * 1_000
                now = loop.time()
                if now - last_display >= display_seconds:
                    print(render_candidates(candidates, settings.scanner.top_limit), flush=True)
                    log.info(
                        "scanner_latency",
                        stage_a_ms=round(stage_a_ms, 3),
                        stage_b_ms=round(stage_b_ms, 3),
                        stage_c_ms=round(stage_c_ms, 3),
                        decision_ms=round(decision_ms, 3),
                        stage_c_candidates=stage_c.last_processed_count,
                    )
                    last_display = now
                await asyncio.sleep(settings.scanner.scan_interval_seconds)

        async def pipeline() -> None:
            async with asyncio.TaskGroup() as group:
                group.create_task(market_ws.run(), name="market-websocket")
                group.create_task(deep_ws.run(), name="deep-websocket")
                group.create_task(oi_poller.run(), name="open-interest-poller")
                group.create_task(scan_loop(), name="stage-a-scanner")

        try:
            if duration_seconds is None:
                await pipeline()
            else:
                try:
                    async with asyncio.timeout(duration_seconds):
                        await pipeline()
                except TimeoutError:
                    log.info("scanner_duration_complete", duration_seconds=duration_seconds)
        finally:
            if recorder is not None:
                await recorder.close()


def render_candidates(candidates: Sequence[Candidate], limit: int) -> str:
    """Render stable plain text suitable for VPS logs. / Рисует таблицу для VPS-логов."""

    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    header = (
        f"\nPumpLens Stage A — {timestamp}\n"
        "SEL SYMBOL          DIR SCORE    1m%    3m%   VOLx  PRESS  TRDx  SPREAD  STATUS"
    )
    rows = [header]
    for candidate in candidates[:limit]:
        item = candidate.snapshot
        selected = " * " if candidate.selected else "   "
        pressure = item.buy_pressure if item.direction.value == "LONG" else 1.0 - item.buy_pressure
        status = candidate.hard_reject_reason or ",".join(candidate.confirmations) or "observing"
        rows.append(
            f"{selected} {item.symbol:<14} {item.direction.value:<5} {item.score:>5.1f} "
            f"{item.return_1m:>6.2f} {item.return_3m:>6.2f} {item.volume_ratio_1m:>6.1f} "
            f"{pressure:>6.0%} {item.trade_rate_ratio:>5.1f} {item.spread_pct:>7.3f}%  "
            f"{status}"
        )
    rows.append("* = Stage B candidate / кандидат Stage B")
    return "\n".join(rows)


async def shutdown_after(task: asyncio.Task[None], seconds: float) -> None:
    """Test helper for bounded runs. / Помощник ограниченного тестового запуска."""

    await asyncio.sleep(seconds)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
