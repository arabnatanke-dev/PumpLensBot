"""Stage A score with explainable components. / Объяснимый score первого этапа."""

from __future__ import annotations

from dataclasses import replace

from pumplens.domain.enums import Direction
from pumplens.domain.models import FeatureSnapshot


def clamp01(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def score_stage_a(snapshot: FeatureSnapshot, max_spread_pct: float) -> FeatureSnapshot:
    """Apply configured initial weights; OI remains for Stage B. / Применяет веса ТЗ."""

    sign = 1.0 if snapshot.direction is Direction.LONG else -1.0
    directional_return = sign * snapshot.return_1m
    directional_acceleration = sign * snapshot.acceleration_1m
    directional_relative = sign * snapshot.relative_strength_1m
    directional_pressure = (
        snapshot.buy_pressure
        if snapshot.direction is Direction.LONG
        else 1.0 - snapshot.buy_pressure
    )

    volume_component = (
        clamp01((snapshot.volume_ratio_1m - 1.0) / 5.0)
        + clamp01(snapshot.volume_robust_z / 5.0)
    ) / 2.0
    movement_component = (
        clamp01(directional_return / 2.5) + clamp01(directional_acceleration / 1.0)
    ) / 2.0
    pressure_component = clamp01((directional_pressure - 0.5) / 0.18)
    trade_component = clamp01((snapshot.trade_rate_ratio - 1.0) / 4.0)
    liquidity_component = clamp01(1.0 - snapshot.spread_pct / max_spread_pct)
    relative_component = clamp01(directional_relative / 2.0)

    # Exact MVP weights from the specification; Stage B later adds OI (12 points).
    # Точные веса MVP из ТЗ; Stage B позже добавляет OI (12 баллов).
    score = (
        25.0 * volume_component
        + 18.0 * movement_component
        + 15.0 * pressure_component
        + 10.0 * trade_component
        + 8.0 * liquidity_component
        + 7.0 * relative_component
        + 5.0 * snapshot.candle_structure
    )

    reasons: list[str] = []
    if snapshot.volume_ratio_1m >= 2.5:
        reasons.append(f"volume {snapshot.volume_ratio_1m:.1f}x")
    if directional_return >= 0.6:
        reasons.append(f"move {directional_return:.2f}%")
    if directional_pressure >= 0.58:
        reasons.append(f"pressure {directional_pressure:.0%}")
    if snapshot.trade_rate_ratio >= 2.0:
        reasons.append(f"trades {snapshot.trade_rate_ratio:.1f}x")
    if directional_relative >= 0.5:
        reasons.append(f"relative {directional_relative:.2f}pp")
    if snapshot.spread_pct <= max_spread_pct:
        reasons.append(f"spread {snapshot.spread_pct:.3f}%")

    return replace(snapshot, score=round(clamp01(score / 100.0) * 100.0, 2), reasons=tuple(reasons))
