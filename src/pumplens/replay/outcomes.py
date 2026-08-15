"""Pure MFE/MAE outcome calculations. / Чистые расчёты результатов MFE/MAE."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pumplens.domain.enums import Direction
from pumplens.domain.models import Kline


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


def evaluate_kline_outcome(
    direction: Direction,
    trigger_price: float,
    klines: Iterable[Kline],
    *,
    target_pct: float = 2.0,
    stop_pct: float = -1.5,
) -> Outcome:
    """Use candle extremes and expose unknown intrabar order. / Использует high/low свечи."""

    if trigger_price <= 0:
        return Outcome(0.0, 0.0, None)
    sign = 1.0 if direction is Direction.LONG else -1.0
    favorable: list[float] = []
    adverse: list[float] = []
    hit_rule: str | None = None
    for kline in klines:
        high_move = (kline.high / trigger_price - 1.0) * 100.0 * sign
        low_move = (kline.low / trigger_price - 1.0) * 100.0 * sign
        best = max(high_move, low_move)
        worst = min(high_move, low_move)
        favorable.append(best)
        adverse.append(worst)
        if hit_rule is None:
            target_hit = best >= target_pct
            stop_hit = worst <= stop_pct
            if target_hit and stop_hit:
                hit_rule = "AMBIGUOUS"
            elif target_hit:
                hit_rule = "TARGET_FIRST"
            elif stop_hit:
                hit_rule = "STOP_FIRST"
    if not favorable:
        return Outcome(0.0, 0.0, None)
    return Outcome(max(favorable), min(adverse), hit_rule)
