"""Domain enumerations. / Доменные перечисления."""

from enum import StrEnum


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalState(StrEnum):
    NORMAL = "NORMAL"
    CANDIDATE = "CANDIDATE"
    EARLY = "EARLY"
    WATCH = "WATCH"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"
    TOO_LATE = "TOO_LATE"
    COOLDOWN = "COOLDOWN"


class DataQuality(StrEnum):
    WARMING_UP = "WARMING_UP"
    FRESH = "FRESH"
    STALE = "STALE"
    GAP = "GAP"


class MarketStructure(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    RANGE = "RANGE"
    NEUTRAL = "NEUTRAL"


class BreakoutState(StrEnum):
    NONE = "NONE"
    IN_PROGRESS = "IN_PROGRESS"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"


class RetestState(StrEnum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_YET = "NOT_YET"
    STARTED = "STARTED"
    HELD = "HELD"
    FAILED = "FAILED"


class EntryDecision(StrEnum):
    ENTER_CANDIDATE = "ENTER_CANDIDATE"
    WAIT_RETEST = "WAIT_RETEST"
    WATCH = "WATCH"
    SKIP_LATE = "SKIP_LATE"
    SKIP_BAD_RR = "SKIP_BAD_RR"
    SKIP_RESISTANCE_TOO_CLOSE = "SKIP_RESISTANCE_TOO_CLOSE"
    SKIP_SUPPORT_TOO_CLOSE = "SKIP_SUPPORT_TOO_CLOSE"
    SKIP_EXHAUSTION = "SKIP_EXHAUSTION"
    SKIP_STRUCTURE_CONFLICT = "SKIP_STRUCTURE_CONFLICT"
    INVALIDATED = "INVALIDATED"
