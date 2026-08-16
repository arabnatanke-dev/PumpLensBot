"""Safe Russian Telegram formatting. / Безопасное русское форматирование Telegram."""

from __future__ import annotations

import html
from collections.abc import Sequence

from pumplens.analytics.early_stats import EarlyStatistics
from pumplens.domain.models import Candidate
from pumplens.storage.models import (
    EarnHoldingRecord,
    FundingHoldingRecord,
    PortfolioSnapshotRecord,
    PositionRecord,
    SpotHoldingRecord,
)


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
    if snapshot.earn_value is not None:
        total += snapshot.earn_value
    if snapshot.funding_value is not None:
        total += snapshot.funding_value
    lines = [
        "<b>💼 Binance Portfolio</b>",
        f"💰 Всего: {total:.2f} USDT",
        f"Spot: {snapshot.spot_value:.2f} USDT",
        f"Futures equity: {snapshot.futures_wallet:.2f} USDT",
        _earn_source_line(snapshot),
        _optional_source_line("Funding", snapshot.funding_value),
        f"Доступно на Futures: {snapshot.available:.2f} USDT",
        f"Нереализованный PnL: {snapshot.unrealized_pnl:+.2f} USDT",
        f"Открытые позиции: {len(positions)}",
        f"Качество данных: {html.escape(snapshot.data_quality)}",
    ]
    unavailable = [
        source
        for source, status in (snapshot.source_status_json or {}).items()
        if status == "UNAVAILABLE"
    ]
    if unavailable:
        lines.append("⚠️ Часть источников недоступна: " + ", ".join(unavailable))
    return "\n".join(lines)


def format_spot(holdings: Sequence[SpotHoldingRecord]) -> str:
    if not holdings:
        return "<b>💰 Spot</b>\nНенулевых активов нет."
    lines = ["<b>💰 Spot</b>"]
    for holding in holdings[:15]:
        amount = holding.free + holding.locked
        lines.append(
            f"• <b>{html.escape(holding.asset)}</b> · {amount:.8f} · "
            f"{_value_text(holding.value_usdt)}"
        )
    return "\n".join(lines)


def format_earn(holdings: Sequence[EarnHoldingRecord], status: dict[str, str]) -> str:
    lines = ["<b>🌱 Simple Earn</b>"]
    if not holdings:
        lines.append("Активных Flexible/Locked позиций нет.")
    for holding in holdings[:15]:
        source = (
            "earn_flexible" if holding.product_type == "FLEXIBLE" else "earn_locked"
        )
        freshness = " · последние данные" if status.get(source) == "UNAVAILABLE" else ""
        lines.append(
            f"• <b>{html.escape(holding.asset)}</b> · {holding.product_type} · "
            f"{holding.amount:.8f} · {_value_text(holding.value_usdt)}{freshness}"
        )
    unavailable_count = sum(
        status.get(source) == "UNAVAILABLE"
        for source in ("earn_flexible", "earn_locked")
    )
    if unavailable_count == 2:
        lines.append("Итого: полностью недоступно.")
    elif unavailable_count == 1:
        lines.append("Итого: PARTIAL — полный total недоступен.")
    _append_source_warnings(lines, status, ("earn_flexible", "earn_locked"))
    return "\n".join(lines)


def format_funding(
    holdings: Sequence[FundingHoldingRecord],
    status: dict[str, str],
) -> str:
    lines = ["<b>👛 Funding Wallet</b>"]
    if not holdings:
        lines.append("Ненулевых активов нет.")
    for holding in holdings[:15]:
        lines.append(
            f"• <b>{html.escape(holding.asset)}</b> · {holding.amount:.8f} · "
            f"{_value_text(holding.value_usdt)}"
        )
    _append_source_warnings(lines, status, ("funding",))
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


def format_futures(
    snapshot: PortfolioSnapshotRecord,
    positions: Sequence[PositionRecord],
) -> str:
    return "\n".join(
        [
            "<b>📊 USDⓈ-M Futures</b>",
            f"Equity (totalMarginBalance): {snapshot.futures_wallet:.2f} USDT",
            f"Доступно: {snapshot.available:.2f} USDT",
            f"Нереализованный PnL: {snapshot.unrealized_pnl:+.2f} USDT",
            "",
            format_positions(positions),
        ]
    )


def _optional_source_line(label: str, value: object | None) -> str:
    return f"{label}: недоступно" if value is None else f"{label}: {value:.2f} USDT"


def _earn_source_line(snapshot: PortfolioSnapshotRecord) -> str:
    status = snapshot.source_status_json or {}
    sources = (status.get("earn_flexible"), status.get("earn_locked"))
    unavailable_count = sources.count("UNAVAILABLE")
    if unavailable_count == 2:
        return "Earn: полностью недоступно"
    if unavailable_count == 1:
        return "Earn: PARTIAL — полный total недоступен"
    suffix = " · PARTIAL" if "PARTIAL" in sources else ""
    return _optional_source_line("Earn", snapshot.earn_value) + suffix


def _value_text(value: object | None) -> str:
    return "оценка недоступна" if value is None else f"≈ {value:.2f} USDT"


def _append_source_warnings(
    lines: list[str],
    status: dict[str, str],
    sources: Sequence[str],
) -> None:
    unavailable = [source for source in sources if status.get(source) == "UNAVAILABLE"]
    if unavailable:
        lines.append("⚠️ Источник сейчас недоступен; показаны последние сохранённые данные.")


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
