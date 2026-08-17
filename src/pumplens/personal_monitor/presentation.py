"""Telegram text for personal scenarios. / Telegram-тексты персональных сценариев."""

from __future__ import annotations

import html
from decimal import Decimal

from pumplens.storage.models import MonitoredScenarioRecord


def format_scenario(record: MonitoredScenarioRecord) -> str:
    symbol = html.escape(record.symbol)
    if record.direction is None:
        return (
            f"<b>📡 {symbol}</b>\n\n"
            "Сейчас качественного сценария нет.\n"
            f"LONG: {_score(record.long_score)}/100\n"
            f"SHORT: {_score(record.short_score)}/100\n\n"
            "Decision: <b>NO TRADE</b>"
        )
    return (
        f"<b>📡 {symbol}</b>\n\n"
        f"Направление: <b>{record.direction}</b>\n"
        f"Сценарий: <b>{record.setup_type}</b>\n"
        f"Signal Score: {_selected_score(record)}/100\n\n"
        f"Уровень: {_price(record.trigger_level)}\n"
        f"Retest zone: {_zone(record)}\n"
        f"Invalidation: {_price(record.invalidation_level)}\n"
        f"Target: {_price(record.target1)}\n\n"
        "Условия подтверждения:\n"
        "• закрытая 1m свеча подтверждает структуру\n"
        "• directional pressure выше порога\n"
        "• объём выше порога"
        + ("\n• OI подтверждает направление" if record.require_oi_confirmation else "")
    )


def format_status(record: MonitoredScenarioRecord) -> str:
    return (
        f"<b>📊 Персональный монитор</b>\n\n"
        f"Symbol: <b>{html.escape(record.symbol)}</b>\n"
        f"Direction: {record.direction or '—'}\n"
        f"Setup: {record.setup_type or '—'}\n"
        f"Current price: {_price(record.latest_price)}\n"
        f"Trigger: {_price(record.trigger_level)}\n"
        f"Retest: {_zone(record)}\n"
        f"Invalidation: {_price(record.invalidation_level)}\n"
        f"State: <b>{record.status}</b>\n"
        "Last evaluated 1m candle: "
        f"{record.last_evaluated_candle_open_time or '—'}"
    )


def format_confirmed(record: MonitoredScenarioRecord) -> str:
    payload = record.analysis_json
    snapshot = payload.get("selected_snapshot", {})
    return (
        f"<b>🟢 СЦЕНАРИЙ ПОДТВЕРЖДЁН — {html.escape(record.symbol)}</b>\n\n"
        f"Direction: <b>{record.direction}</b>\n"
        f"Цена: {_price(record.latest_price)}\n"
        f"Уровень: {_price(record.trigger_level)}\n"
        f"Volume: {_payload_float(snapshot.get('volume_ratio_1m')):.2f}x\n"
        f"Pressure: {_payload_float(snapshot.get('directional_pressure')):.0%}\n"
        f"OI: {_payload_float(snapshot.get('oi_delta_pct')):+.3f}%"
    )


def format_changed(
    old: MonitoredScenarioRecord,
    replacement: MonitoredScenarioRecord,
) -> str:
    decision = (
        f"WATCH {replacement.direction}"
        if replacement.direction is not None
        else "NO TRADE"
    )
    return (
        f"<b>🔄 СЦЕНАРИЙ ИЗМЕНИЛСЯ — {html.escape(old.symbol)}</b>\n\n"
        f"Старый: {old.direction} ❌\n"
        "Причина: 1m close за invalidation.\n\n"
        f"LONG: {_score(replacement.long_score)}/100\n"
        f"SHORT: {_score(replacement.short_score)}/100\n\n"
        f"Decision: <b>{decision}</b>"
    )


def _selected_score(record: MonitoredScenarioRecord) -> str:
    return _score(record.long_score if record.direction == "LONG" else record.short_score)


def _score(value: Decimal | float | None) -> str:
    return f"{float(value):.1f}" if value is not None else "0.0"


def _price(value: Decimal | float | None) -> str:
    if value is None:
        return "—"
    number = float(value)
    return f"{number:.8f}".rstrip("0").rstrip(".")


def _zone(record: MonitoredScenarioRecord) -> str:
    if record.retest_zone_low is None or record.retest_zone_high is None:
        return "—"
    return f"{_price(record.retest_zone_low)}–{_price(record.retest_zone_high)}"


def _payload_float(value: object) -> float:
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return 0.0
