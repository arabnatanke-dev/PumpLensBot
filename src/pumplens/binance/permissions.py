"""Fail-closed Binance API permission policy. / Fail-closed политика прав Binance API."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

FORBIDDEN_TRUE_FIELDS = (
    "enableWithdrawals",
    "enableInternalTransfer",
    "permitsUniversalTransfer",
    "enableMargin",
    "enableSpotAndMarginTrading",
    "enableFutures",
    "enablePortfolioMarginTrading",
)


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    accepted: bool
    reason: str


def validate_read_only_permissions(payload: Mapping[str, Any]) -> PermissionDecision:
    """Unknown or missing fields reject the key. / Неизвестный ответ отклоняет ключ."""

    if payload.get("enableReading") is not True:
        return PermissionDecision(False, "reading_not_enabled")
    missing = [field for field in FORBIDDEN_TRUE_FIELDS if field not in payload]
    if missing:
        return PermissionDecision(False, "permissions_response_incomplete")
    enabled = [field for field in FORBIDDEN_TRUE_FIELDS if payload.get(field) is True]
    if enabled:
        return PermissionDecision(False, f"dangerous_permission:{enabled[0]}")
    return PermissionDecision(True, "read_only")
