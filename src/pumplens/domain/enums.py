"""Domain enumerations. / Доменные перечисления."""

from enum import StrEnum


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalState(StrEnum):
    NORMAL = "NORMAL"
    CANDIDATE = "CANDIDATE"
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
