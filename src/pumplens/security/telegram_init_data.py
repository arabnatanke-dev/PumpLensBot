"""Server-side Telegram Mini App validation. / Серверная проверка Telegram Mini App."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


class TelegramInitDataError(ValueError):
    """Validation error safe to show to the client. / Безопасная ошибка проверки."""


@dataclass(frozen=True, slots=True)
class VerifiedTelegramUser:
    user_id: int
    first_name: str
    last_name: str | None
    username: str | None
    language_code: str | None
    auth_date: int


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 300,
    now: int | None = None,
) -> VerifiedTelegramUser:
    """Verify HMAC, age, and user shape. / Проверяет HMAC, возраст и объект user."""

    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    values = dict(pairs)
    received_hash = values.pop("hash", None)
    if not received_hash:
        raise TelegramInitDataError("Missing Telegram hash")
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise TelegramInitDataError("Invalid Telegram signature")

    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TelegramInitDataError("Invalid Telegram user data") from exc

    current = int(time.time()) if now is None else now
    if auth_date > current + 30 or current - auth_date > max_age_seconds:
        raise TelegramInitDataError("Telegram init data expired")
    return VerifiedTelegramUser(
        user_id=user_id,
        first_name=str(user.get("first_name", "")),
        last_name=_optional_string(user.get("last_name")),
        username=_optional_string(user.get("username")),
        language_code=_optional_string(user.get("language_code")),
        auth_date=auth_date,
    )


def _optional_string(value: object) -> str | None:
    return str(value) if value not in (None, "") else None
