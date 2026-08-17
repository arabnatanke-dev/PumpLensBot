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


class EarlyLiquidityTier(StrictModel):
    name: str
    min_quote_volume_24h: float = Field(ge=0.0)
    min_quote_volume_1m: PositiveFloat
    min_trade_count_1m: PositiveInt


class EarlySettings(StrictModel):
    enabled: bool = True
    shadow_mode: bool = True
    min_score: float = Field(default=58.0, ge=0.0, le=100.0)
    max_score: float = Field(default=69.99, ge=0.0, le=100.0)
    min_volume_ratio: PositiveFloat = 5.0
    min_trade_rate_ratio: PositiveFloat = 5.0
    min_pressure: float = Field(default=0.72, ge=0.5, le=1.0)
    min_volume_z: float = Field(default=4.0, ge=0.0)
    min_return_1m_pct: PositiveFloat = 0.15
    max_return_1m_pct: PositiveFloat = 1.50
    max_return_5m_pct: PositiveFloat = 4.0
    max_spread_pct: PositiveFloat = 0.20
    min_quote_volume_1m: PositiveFloat = 5_000.0
    min_trade_count_1m: PositiveInt = 20
    debounce_hits: PositiveInt = 2
    debounce_window_seconds: PositiveInt = 3
    invalidate_after_seconds: PositiveInt = 12
    pressure_reversal: float = Field(default=0.45, ge=0.0, le=0.5)
    against_move_pct: PositiveFloat = 0.25
    rearm_seconds: PositiveInt = 60
    outcome_poll_seconds: PositiveFloat = 5.0
    liquidity_tiers: tuple[EarlyLiquidityTier, ...] = (
        EarlyLiquidityTier(
            name="thin",
            min_quote_volume_24h=2_000_000,
            min_quote_volume_1m=5_000,
            min_trade_count_1m=20,
        ),
        EarlyLiquidityTier(
            name="mid",
            min_quote_volume_24h=25_000_000,
            min_quote_volume_1m=15_000,
            min_trade_count_1m=40,
        ),
        EarlyLiquidityTier(
            name="liquid",
            min_quote_volume_24h=250_000_000,
            min_quote_volume_1m=50_000,
            min_trade_count_1m=100,
        ),
    )

    @model_validator(mode="after")
    def validate_early_thresholds(self) -> EarlySettings:
        if self.min_score >= self.max_score:
            raise ValueError("early min_score must be lower than max_score")
        if self.min_return_1m_pct >= self.max_return_1m_pct:
            raise ValueError("early min_return_1m_pct must be lower than max_return_1m_pct")
        if self.debounce_hits > self.debounce_window_seconds:
            raise ValueError("early debounce_hits must not exceed debounce_window_seconds")
        tier_starts = [tier.min_quote_volume_24h for tier in self.liquidity_tiers]
        if tier_starts != sorted(tier_starts) or len(set(tier_starts)) != len(tier_starts):
            raise ValueError("early liquidity_tiers must have unique ascending 24h thresholds")
        return self


class LateSettings(StrictModel):
    return_5m_pct: PositiveFloat = 8.0
    return_15m_pct: PositiveFloat = 15.0
    range_1m_pct: PositiveFloat = 6.0


class StageCWeightSettings(StrictModel):
    base_quality: float = Field(default=35.0, ge=0.0, le=100.0)
    structure_aligned: PositiveFloat = 12.0
    breakout_confirmed: PositiveFloat = 10.0
    retest_held: PositiveFloat = 14.0
    volume_confirmation: PositiveFloat = 6.0
    trades_confirmation: PositiveFloat = 5.0
    pressure_confirmation: PositiveFloat = 7.0
    oi_confirmation: PositiveFloat = 4.0
    spread_healthy: PositiveFloat = 4.0
    depth_supportive: PositiveFloat = 4.0
    room_available: PositiveFloat = 7.0
    rr_acceptable: PositiveFloat = 8.0
    late_penalty: PositiveFloat = 28.0
    exhaustion_penalty: PositiveFloat = 24.0
    failed_breakout_penalty: PositiveFloat = 30.0
    structure_conflict_penalty: PositiveFloat = 20.0
    vwap_distance_penalty: PositiveFloat = 10.0
    opposing_wick_penalty: PositiveFloat = 8.0


class StageCSettings(StrictModel):
    enabled: bool = True
    require_for_watch: bool = False
    max_candidates: PositiveInt = Field(default=8, le=30)
    structure_lookback: PositiveInt = Field(default=120, ge=30, le=180)
    pivot_window: PositiveInt = Field(default=2, le=10)
    level_lookback: PositiveInt = Field(default=120, ge=20, le=180)
    level_cluster_tolerance_pct: PositiveFloat = 0.25
    level_min_touches: PositiveInt = Field(default=1, le=10)
    retest_tolerance_pct: PositiveFloat = 0.20
    atr_period: PositiveInt = Field(default=14, ge=5, le=60)
    late_atr_multiplier: PositiveFloat = 3.0
    late_score_threshold: float = Field(default=70.0, ge=0.0, le=100.0)
    exhaustion_threshold: float = Field(default=70.0, ge=0.0, le=100.0)
    min_rr: PositiveFloat = 1.5
    min_entry_quality: float = Field(default=55.0, ge=0.0, le=100.0)
    max_distance_from_vwap_pct: PositiveFloat = 3.0
    min_room_pct: PositiveFloat = 0.50
    min_volume_confirmation: PositiveFloat = 2.5
    min_trade_confirmation: PositiveFloat = 2.0
    min_pressure_confirmation: float = Field(default=0.58, ge=0.5, le=1.0)
    healthy_spread_pct: PositiveFloat = 0.15
    min_depth_imbalance: float = Field(default=0.10, ge=0.0, le=1.0)
    min_oi_delta_pct: float = Field(default=0.05, ge=0.0)
    max_opposing_wick_ratio: float = Field(default=0.45, gt=0.0, lt=1.0)
    volatility_buffer_atr: PositiveFloat = 0.20
    target_atr_multiplier: PositiveFloat = 2.0
    compression_ratio: float = Field(default=0.70, gt=0.0, lt=1.0)
    expansion_ratio: PositiveFloat = 1.50
    weights: StageCWeightSettings = StageCWeightSettings()

    @model_validator(mode="after")
    def validate_stage_c(self) -> StageCSettings:
        if self.pivot_window * 2 + 1 >= self.structure_lookback:
            raise ValueError("stage_c pivot window must fit structure lookback")
        return self


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
    live_mark_stale_seconds: PositiveFloat = 5.0
    liquidation_warning_pct: PositiveFloat = 5.0
    pnl_milestones_pct: tuple[PositiveFloat, ...] = (5.0, 8.0, 10.0, 15.0, 20.0)


class ReplaySettings(StrictModel):
    enabled: bool = True
    record_path: str = "data/market.jsonl"
    queue_size: PositiveInt = 100_000
    batch_size: PositiveInt = 500
    flush_interval_seconds: PositiveFloat = 0.5
    max_file_size_mb: PositiveInt = 200
    retention_days: PositiveInt = 10
    gzip_rotated: bool = True
    record_book_ticker: bool = False


class NotificationSettings(StrictModel):
    edit_debounce_seconds: PositiveInt = 4
    min_score: float = Field(default=70.0, ge=0.0, le=100.0)
    directions: tuple[Literal["LONG", "SHORT"], ...] = ("LONG", "SHORT")


class TelegramSettings(StrictModel):
    signal_ttl_hours: PositiveInt = 47
    cleanup_scan_seconds: PositiveFloat = 60.0


class PersonalMonitorSettings(StrictModel):
    """Bounded runtime for user-selected symbols. / Ограниченный runtime выбранных монет."""

    enabled: bool = True
    discovery_seconds: PositiveFloat = 3.0
    warmup_klines: PositiveInt = Field(default=180, ge=30, le=180)
    max_active_symbols: PositiveInt = Field(default=50, le=200)
    exchange_info_ttl_seconds: PositiveInt = 900


class AppSettings(StrictModel):
    binance: BinanceSettings = BinanceSettings()
    scanner: ScannerSettings = ScannerSettings()
    early: EarlySettings = EarlySettings()
    late: LateSettings = LateSettings()
    stage_c: StageCSettings = StageCSettings()
    oi: OpenInterestSettings = OpenInterestSettings()
    onboarding: OnboardingSettings = OnboardingSettings()
    portfolio: PortfolioSettings = PortfolioSettings()
    replay: ReplaySettings = ReplaySettings()
    notifications: NotificationSettings = NotificationSettings()
    telegram: TelegramSettings = TelegramSettings()
    personal_monitor: PersonalMonitorSettings = PersonalMonitorSettings()


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
