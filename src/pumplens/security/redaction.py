"""Recursive secret redaction for logs. / Рекурсивное удаление секретов из логов."""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping
from typing import Any, cast

SENSITIVE_KEY = re.compile(
    r"(secret|api[_-]?key|token|authorization|signature|credential|password|ciphertext|nonce)",
    re.IGNORECASE,
)
BEARER = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")


def redact(value: Any) -> Any:
    """Return a safe copy suitable for logs. / Возвращает безопасную копию для логов."""

    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if SENSITIVE_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return BEARER.sub("Bearer [REDACTED]", value)
    return value


def structlog_redactor(
    _: object,
    __: str,
    event_dict: MutableMapping[str, Any],
) -> Mapping[str, Any]:
    """Structlog processor. / Процессор structlog."""

    return cast(dict[str, Any], redact(dict(event_dict)))
