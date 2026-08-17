"""Closed-candle scenario rules. / Правила сценария только по закрытым свечам."""

from __future__ import annotations

from decimal import Decimal

from pumplens.config import StageCSettings
from pumplens.domain.enums import Direction
from pumplens.domain.models import FeatureSnapshot, Kline
from pumplens.personal_monitor.domain import EvaluationDecision, SetupType
from pumplens.storage.models import MonitoredScenarioRecord


def evaluate_scenario(
    scenario: MonitoredScenarioRecord,
    candle: Kline,
    snapshot: FeatureSnapshot,
    settings: StageCSettings,
) -> tuple[EvaluationDecision, str]:
    """Return exactly WAIT, CONFIRMED, or INVALIDATED. / Возвращает ровно три исхода."""

    if not candle.closed or scenario.direction is None:
        return EvaluationDecision.WAIT, "CANDLE_NOT_CLOSED"
    direction = Direction(scenario.direction)
    invalidation = _required_float(scenario.invalidation_level)
    trigger = _required_float(scenario.trigger_level)
    if (direction is Direction.LONG and candle.close < invalidation) or (
        direction is Direction.SHORT and candle.close > invalidation
    ):
        return EvaluationDecision.INVALIDATED, "CLOSE_BEYOND_INVALIDATION"

    directional_close = (
        candle.close > trigger
        if direction is Direction.LONG
        else candle.close < trigger
    )
    if directional_close:
        scenario.breakout_observed = True

    zone_touched = _zone_touched(scenario, candle)
    if scenario.breakout_observed and zone_touched:
        scenario.retest_observed = True

    setup = SetupType(str(scenario.setup_type))
    pattern_ready = _pattern_ready(
        setup,
        directional_close=directional_close,
        breakout_observed=scenario.breakout_observed,
        retest_observed=scenario.retest_observed,
        zone_touched=zone_touched,
    )
    if pattern_ready and _confirmations_pass(scenario, snapshot, direction, settings):
        return EvaluationDecision.CONFIRMED, "CLOSED_CANDLE_CONFIRMED"
    return EvaluationDecision.WAIT, "CONDITIONS_PENDING"


def _pattern_ready(
    setup: SetupType,
    *,
    directional_close: bool,
    breakout_observed: bool,
    retest_observed: bool,
    zone_touched: bool,
) -> bool:
    if setup is SetupType.BREAK_RETEST:
        return breakout_observed and retest_observed and directional_close
    if setup is SetupType.BREAKOUT:
        return breakout_observed and directional_close
    return zone_touched and directional_close


def _confirmations_pass(
    scenario: MonitoredScenarioRecord,
    snapshot: FeatureSnapshot,
    direction: Direction,
    settings: StageCSettings,
) -> bool:
    pressure = snapshot.buy_pressure if direction is Direction.LONG else 1.0 - snapshot.buy_pressure
    if (
        scenario.require_volume_confirmation
        and snapshot.volume_ratio_1m < settings.min_volume_confirmation
    ):
        return False
    if (
        scenario.require_pressure_confirmation
        and pressure < settings.min_pressure_confirmation
    ):
        return False
    directional_oi = (
        snapshot.oi_delta_pct
        if direction is Direction.LONG
        else -snapshot.oi_delta_pct
    )
    return not scenario.require_oi_confirmation or (
        snapshot.oi_data_ready and directional_oi >= settings.min_oi_delta_pct
    )


def _zone_touched(scenario: MonitoredScenarioRecord, candle: Kline) -> bool:
    if scenario.retest_zone_low is None or scenario.retest_zone_high is None:
        return False
    low = float(scenario.retest_zone_low)
    high = float(scenario.retest_zone_high)
    return candle.low <= high and candle.high >= low


def _required_float(value: Decimal | float | None) -> float:
    if value is None:
        raise ValueError("scenario level is required")
    return float(value)
