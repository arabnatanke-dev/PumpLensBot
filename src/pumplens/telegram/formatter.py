"""Safe Russian Telegram formatting. / Безопасное русское форматирование Telegram."""

from __future__ import annotations

import html
from collections.abc import Sequence

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
