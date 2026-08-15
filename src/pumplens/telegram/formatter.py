"""Safe Russian Telegram formatting. / Безопасное русское форматирование Telegram."""

from __future__ import annotations

import html
from collections.abc import Sequence

from pumplens.analytics.early_stats import EarlyStatistics
from pumplens.domain.models import Candidate
from pumplens.storage.models import PortfolioSnapshotRecord, PositionRecord


def format_top(candidates: Sequence[Candidate]) -> str:
    if not candidates:
        return "Сканер прогревается. Попробуйте /top через минуту."
    lines = ["<b>Топ рынка PumpLens</b>"]
    for index, candidate in enumerate(candidates, 1):
        item = candidate.snapshot
        marker = "🟠" if candidate.selected else "⚪️"
        lines.append(
            f"{index}. {marker} <b>{html.escape(item.symbol)}</b> {item.direction.value} "
            f"— {item.score:.0f}/100 · 1м {item.return_1m:+.2f}% · "
            f"объём {item.volume_ratio_1m:.1f}×"
        )
    lines.append("\nScore показывает совпадение признаков, а не вероятность прибыли.")
    return "\n".join(lines)


def format_portfolio(
    snapshot: PortfolioSnapshotRecord,
    positions: Sequence[PositionRecord],
) -> str:
    total = snapshot.spot_value + snapshot.futures_wallet
    lines = [
        "<b>💼 Binance Portfolio</b>",
        f"Общая стоимость: {total:.2f} USDT",
        f"Spot: {snapshot.spot_value:.2f} USDT",
        f"Futures wallet: {snapshot.futures_wallet:.2f} USDT",
        f"Доступно на Futures: {snapshot.available:.2f} USDT",
        f"Нереализованный PnL: {snapshot.unrealized_pnl:+.2f} USDT",
        f"Открытые позиции: {len(positions)}",
        f"Качество данных: {html.escape(snapshot.data_quality)}",
    ]
    return "\n".join(lines)


def format_positions(positions: Sequence[PositionRecord]) -> str:
    if not positions:
        return "Открытых Futures-позиций нет."
    lines = ["<b>Открытые Futures-позиции</b>"]
    for position in positions:
        liquidation = f"{position.liquidation}" if position.liquidation is not None else "—"
        lines.append(
            f"• <b>{html.escape(position.symbol)}</b> {position.side} x{position.leverage} · "
            f"PnL {position.pnl:+.2f} USDT · Liq {liquidation}"
        )
    return "\n".join(lines)


def format_early_statistics(stats: EarlyStatistics) -> str:
    if stats.total == 0:
        return "<b>⚡ EARLY shadow</b>\nДанных пока нет."
    watch_lead = (
        f"{stats.average_watch_lead_seconds:.1f}с"
        if stats.average_watch_lead_seconds is not None
        else "—"
    )
    confirmed_lead = (
        f"{stats.average_confirmed_lead_seconds:.1f}с"
        if stats.average_confirmed_lead_seconds is not None
        else "—"
    )
    lines = [
        "<b>⚡ EARLY shadow</b>",
        f"Событий: {stats.total} · {stats.early_per_hour:.2f}/час",
        f"EARLY → WATCH: {stats.watch_conversion_pct:.1f}%",
        f"EARLY → CONFIRMED: {stats.confirmed_conversion_pct:.1f}%",
        f"EARLY → INVALIDATED: {stats.invalidated_pct:.1f}%",
        f"Средняя фора до WATCH: {watch_lead}",
        f"Средняя фора до CONFIRMED: {confirmed_lead}",
    ]
    if stats.average_returns_pct:
        returns = " · ".join(
            f"{label} {value:+.2f}%" for label, value in stats.average_returns_pct.items()
        )
        lines.append(f"Среднее движение: {returns}")
    if stats.average_mfe_pct is not None and stats.average_mae_pct is not None:
        lines.append(
            f"MFE/MAE: {stats.average_mfe_pct:+.2f}% / {stats.average_mae_pct:+.2f}%"
        )
    for tier, group in stats.liquidity_groups.items():
        lines.append(
            f"{html.escape(tier)}: {group.total} · W {group.watch_conversion_pct:.0f}% · "
            f"C {group.confirmed_conversion_pct:.0f}% · X {group.invalidated_pct:.0f}%"
        )
    return "\n".join(lines)
