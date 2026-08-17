"""Pure personal-monitor domain contracts. / Чистые контракты персонального монитора."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pumplens.domain.models import EntryAnalysis, FeatureSnapshot, PriceZone


class ScenarioStatus(StrEnum):
    SEARCHING = "SEARCHING"
    SCENARIO_FOUND = "SCENARIO_FOUND"
    WAITING = "WAITING"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"
    REASSESS = "REASSESS"
    STOPPED = "STOPPED"


class SetupType(StrEnum):
    BREAK_RETEST = "BREAK_RETEST"
    BREAKOUT = "BREAKOUT"
    SUPPORT_BOUNCE = "SUPPORT_BOUNCE"
    RESISTANCE_REJECTION = "RESISTANCE_REJECTION"


class EvaluationDecision(StrEnum):
    WAIT = "WAIT"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True, slots=True)
class ScenarioPlan:
    direction: str
    setup_type: SetupType
    trigger_level: float
    retest_zone: PriceZone | None
    invalidation_level: float
    target1: float
    require_oi_confirmation: bool
    breakout_observed: bool
    analysis: EntryAnalysis
    snapshot: FeatureSnapshot


@dataclass(frozen=True, slots=True)
class FullAnalysisResult:
    symbol: str
    price: float
    long_score: float
    short_score: float
    long_snapshot: FeatureSnapshot
    short_snapshot: FeatureSnapshot
    long_analysis: EntryAnalysis | None
    short_analysis: EntryAnalysis | None
    plan: ScenarioPlan | None
    data_quality: str
