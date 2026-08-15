"""Pure MFE/MAE outcome calculations. / Чистые расчёты результатов MFE/MAE."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pumplens.domain.enums import Direction


@dataclass(frozen=True, slots=True)
class Outcome:
    mfe_pct: float
    mae_pct: float
    hit_rule: str | None


def evaluate_outcome(
    direction: Direction,
    trigger_price: float,
    prices: Iterable[float],
    *,
    target_pct: float = 2.0,
    stop_pct: float = -1.5,
) -> Outcome:
    sign = 1.0 if direction is Direction.LONG else -1.0
    moves = [
        (price / trigger_price - 1.0) * 100.0 * sign
        for price in prices
        if price > 0 and trigger_price > 0
    ]
    if not moves:
        return Outcome(0.0, 0.0, None)
    hit_rule = None
    for move in moves:
        if move >= target_pct:
            hit_rule = "TARGET_FIRST"
            break
        if move <= stop_pct:
            hit_rule = "STOP_FIRST"
            break
    return Outcome(mfe_pct=max(moves), mae_pct=min(moves), hit_rule=hit_rule)
