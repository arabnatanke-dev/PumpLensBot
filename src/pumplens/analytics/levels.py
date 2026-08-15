"""Human-readable signal levels without false precision. / Уровни без ложной точности."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pumplens.config import LateSettings
from pumplens.domain.enums import Direction
from pumplens.domain.models import Kline


@dataclass(frozen=True, slots=True)
class SignalLevels:
    start_price: float
    trigger_price: float
    invalidation: float
    late_line_5m: float
    late_line_15m: float
    risk_distance_pct: float


def calculate_levels(
    direction: Direction,
    start_price: float,
    trigger_price: float,
    recent_closed: Sequence[Kline],
    late: LateSettings,
) -> SignalLevels:
    recent = recent_closed[-3:]
    if direction is Direction.LONG:
        invalidation = min((bar.low for bar in recent), default=start_price)
        late_5m = start_price * (1.0 + late.return_5m_pct / 100.0)
        late_15m = start_price * (1.0 + late.return_15m_pct / 100.0)
    else:
        invalidation = max((bar.high for bar in recent), default=start_price)
        late_5m = start_price * (1.0 - late.return_5m_pct / 100.0)
        late_15m = start_price * (1.0 - late.return_15m_pct / 100.0)
    risk_distance = abs(trigger_price / invalidation - 1.0) * 100.0 if invalidation else 0.0
    return SignalLevels(
        start_price=start_price,
        trigger_price=trigger_price,
        invalidation=invalidation,
        late_line_5m=late_5m,
        late_line_15m=late_15m,
        risk_distance_pct=risk_distance,
    )
