"""Typed configuration loading. / Типизированная загрузка конфигурации."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class StrictModel(BaseModel):
    """Reject unknown settings to catch typos. / Запрещает неизвестные настройки."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class BinanceSettings(StrictModel):
    rest_base_url: str = "https://fapi.binance.com"
    websocket_base_url: str = "wss://fstream.binance.com"
    request_timeout_seconds: PositiveFloat = 15.0
    warmup_concurrency: PositiveInt = 8
    websocket_streams_per_connection: PositiveInt = 180
    reconnect_max_seconds: PositiveFloat = 30.0
    planned_rotation_seconds: PositiveFloat = 85_500.0
    stale_after_seconds: PositiveFloat = 12.0

    @model_validator(mode="after")
    def validate_stream_limit(self) -> BinanceSettings:
        # Binance permits at most 1024 streams; leave room for control traffic.
        # Binance допускает максимум 1024 потока; оставляем запас для управления.
        if self.websocket_streams_per_connection > 900:
            raise ValueError("websocket_streams_per_connection must be <= 900")
        return self


class ScannerSettings(StrictModel):
    profile: Literal["safe", "balanced", "wild"] = "balanced"
    min_quote_volume_24h: PositiveFloat = 2_000_000.0
    max_spread_pct: PositiveFloat = 0.35
    hard_reject_spread_pct: PositiveFloat = 0.70
    max_deep_candidates: PositiveInt = 30
    candidate_score: float = Field(default=55.0, ge=0.0, le=100.0)
    watch_score: float = Field(default=70.0, ge=0.0, le=100.0)
    confirmed_score: float = Field(default=82.0, ge=0.0, le=100.0)
    cooldown_minutes: PositiveInt = 20
    warmup_klines: PositiveInt = Field(default=120, ge=30, le=1_500)
    scan_interval_seconds: PositiveFloat = 1.0
    top_limit: PositiveInt = Field(default=10, le=100)

    @model_validator(mode="after")
    def validate_threshold_order(self) -> ScannerSettings:
        # State thresholds must be monotonic. / Пороги состояний должны возрастать.
        if not self.candidate_score < self.watch_score < self.confirmed_score:
            raise ValueError("score thresholds must satisfy candidate < watch < confirmed")
        if self.hard_reject_spread_pct <= self.max_spread_pct:
            raise ValueError("hard_reject_spread_pct must exceed max_spread_pct")
        return self


class LateSettings(StrictModel):
    return_5m_pct: PositiveFloat = 8.0
    return_15m_pct: PositiveFloat = 15.0
    range_1m_pct: PositiveFloat = 6.0


class OpenInterestSettings(StrictModel):
    poll_seconds: PositiveInt = 30


class OnboardingSettings(StrictModel):
    invite_only: bool = True
    connect_session_ttl_seconds: PositiveInt = 600
    privacy_version: str = "1.0"


class PortfolioSettings(StrictModel):
    rest_reconcile_seconds: PositiveInt = 300
    listen_key_keepalive_seconds: PositiveInt = 2_700
    max_private_accounts_per_instance: PositiveInt = 50
    account_discovery_seconds: PositiveFloat = 5.0
    event_reconcile_debounce_seconds: PositiveFloat = 1.0
    stale_after_seconds: PositiveInt = 600
    risk_scan_seconds: PositiveFloat = 5.0
    liquidation_warning_pct: PositiveFloat = 5.0
    pnl_milestones_pct: tuple[PositiveFloat, ...] = (5.0, 8.0, 10.0, 15.0, 20.0)


class ReplaySettings(StrictModel):
    enabled: bool = True
    record_path: str = "data/market.jsonl"


class NotificationSettings(StrictModel):
    edit_debounce_seconds: PositiveInt = 4
    min_score: float = Field(default=70.0, ge=0.0, le=100.0)
    directions: tuple[Literal["LONG", "SHORT"], ...] = ("LONG", "SHORT")


class AppSettings(StrictModel):
    binance: BinanceSettings = BinanceSettings()
    scanner: ScannerSettings = ScannerSettings()
    late: LateSettings = LateSettings()
    oi: OpenInterestSettings = OpenInterestSettings()
    onboarding: OnboardingSettings = OnboardingSettings()
    portfolio: PortfolioSettings = PortfolioSettings()
    replay: ReplaySettings = ReplaySettings()
    notifications: NotificationSettings = NotificationSettings()


class RuntimeSecrets(BaseSettings):
    """Secrets are loaded separately and never serialized. / Секреты загружаются отдельно."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: SecretStr | None = Field(
        default=None,
        validation_alias="TELEGRAM_BOT_TOKEN",
    )
    database_url: SecretStr | None = Field(default=None, validation_alias="DATABASE_URL")
    credential_master_key: SecretStr | None = Field(
        default=None,
        validation_alias="CREDENTIAL_MASTER_KEY",
    )
    public_base_url: str | None = Field(default=None, validation_alias="PUBLIC_BASE_URL")
    admin_telegram_user_ids: str = Field(
        default="",
        validation_alias="ADMIN_TELEGRAM_USER_IDS",
    )
    health_host: str = Field(default="0.0.0.0", validation_alias="HEALTH_HOST")
    health_port: int = Field(default=8080, validation_alias="HEALTH_PORT", ge=1, le=65_535)

    @property
    def admin_ids(self) -> frozenset[int]:
        return frozenset(
            int(value.strip())
            for value in self.admin_telegram_user_ids.split(",")
            if value.strip()
        )


def load_settings(path: str | Path | None = None) -> AppSettings:
    """Load YAML and validate every value. / Загружает YAML и проверяет все значения."""

    configured_path: str | Path = (
        path if path is not None else (os.getenv("SETTINGS_FILE") or "settings.yaml")
    )
    settings_path = Path(configured_path)
    if not settings_path.is_file():
        raise FileNotFoundError(
            f"Settings file not found / Файл настроек не найден: {settings_path}"
        )

    raw = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("Settings root must be a mapping / Корень настроек должен быть объектом")
    return AppSettings.model_validate(raw)
