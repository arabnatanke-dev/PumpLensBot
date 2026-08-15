"""Relational schema from the technical specification. / Реляционная схема из ТЗ."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared SQLAlchemy metadata. / Общие metadata SQLAlchemy."""


class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SymbolRecord(Base):
    __tablename__ = "symbols"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    contract_type: Mapped[str] = mapped_column(String(32), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    filters_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SignalRunRecord(UUIDPrimaryKey, Base):
    __tablename__ = "signal_runs"

    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    settings_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SignalRecord(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "signals"
    __table_args__ = (
        Index(
            "uq_signals_active_symbol_direction",
            "symbol",
            "direction",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index("ix_signals_symbol_direction_created", "symbol", "direction", "created_at"),
    )

    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("signal_runs.id"))
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    start_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    trigger_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    invalidation: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    late_line: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    candidate_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SignalFeatureRecord(UUIDPrimaryKey, Base):
    __tablename__ = "signal_features"
    __table_args__ = (Index("ix_signal_features_signal_ts", "signal_id", "ts"),)

    signal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("signals.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    features_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    penalties_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    raw_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)


class SignalEventRecord(UUIDPrimaryKey, Base):
    __tablename__ = "signal_events"
    __table_args__ = (Index("ix_signal_events_signal_ts", "signal_id", "ts"),)

    signal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("signals.id"), nullable=False)
    from_state: Mapped[str] = mapped_column(String(24), nullable=False)
    to_state: Mapped[str] = mapped_column(String(24), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_text: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SignalOutcomeRecord(UUIDPrimaryKey, Base):
    __tablename__ = "signal_outcomes"

    signal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("signals.id"),
        nullable=False,
        unique=True,
    )
    price_5m: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    price_15m: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    price_60m: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    mfe: Mapped[float | None] = mapped_column(Numeric(8, 4))
    mae: Mapped[float | None] = mapped_column(Numeric(8, 4))
    hit_rule: Mapped[str | None] = mapped_column(String(32))
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserRecord(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "users"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="USER", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="ONBOARDING", nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="ru", nullable=False)
    onboarding_state: Mapped[str] = mapped_column(
        String(32),
        default="WELCOME",
        nullable=False,
    )


class UserConsentRecord(UUIDPrimaryKey, Base):
    __tablename__ = "user_consents"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    privacy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    terms_version: Mapped[str] = mapped_column(String(32), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    telegram_metadata_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class UserPreferenceRecord(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
        unique=True,
    )
    profile: Mapped[str] = mapped_column(String(16), default="balanced", nullable=False)
    directions: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["LONG", "SHORT"])
    min_score: Mapped[float] = mapped_column(Numeric(6, 2), default=70, nullable=False)
    quiet_hours: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    watchlist_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class InviteCodeRecord(UUIDPrimaryKey, Base):
    __tablename__ = "invite_codes"

    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ExchangeAccountRecord(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "exchange_accounts"
    __table_args__ = (UniqueConstraint("user_id", "exchange", name="uq_user_exchange"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    exchange: Mapped[str] = mapped_column(String(24), default="BINANCE", nullable=False)
    label: Mapped[str] = mapped_column(String(128), default="Binance Main", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)
    permissions_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    key_last4: Mapped[str] = mapped_column(String(4), nullable=False)


class EncryptedCredentialRecord(Base):
    __tablename__ = "encrypted_credentials"

    exchange_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exchange_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PortfolioSnapshotRecord(UUIDPrimaryKey, Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (Index("ix_portfolio_account_ts", "exchange_account_id", "ts"),)

    exchange_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exchange_accounts.id"),
        nullable=False,
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    spot_value: Mapped[Decimal] = mapped_column(Numeric(38, 18), default=0)
    futures_wallet: Mapped[Decimal] = mapped_column(Numeric(38, 18), default=0)
    available: Mapped[Decimal] = mapped_column(Numeric(38, 18), default=0)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(38, 18), default=0)
    data_quality: Mapped[str] = mapped_column(String(24), nullable=False)


class SpotHoldingRecord(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "spot_holdings"
    __table_args__ = (
        UniqueConstraint("exchange_account_id", "asset", name="uq_spot_account_asset"),
        Index("ix_spot_account_asset", "exchange_account_id", "asset"),
    )

    exchange_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exchange_accounts.id"),
        nullable=False,
    )
    asset: Mapped[str] = mapped_column(String(32), nullable=False)
    free: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    locked: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    value_usdt: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))


class PositionRecord(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint(
            "exchange_account_id",
            "symbol",
            "side",
            name="uq_position_account_symbol_side",
        ),
        Index("ix_positions_account_symbol_side", "exchange_account_id", "symbol", "side"),
    )

    exchange_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exchange_accounts.id"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    entry: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    break_even: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    mark: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    liquidation: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    leverage: Mapped[int] = mapped_column(Integer, nullable=False)
    margin_type: Mapped[str] = mapped_column(String(16), nullable=False)
    pnl: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)


class RiskAlertRecord(UUIDPrimaryKey, Timestamped, Base):
    """Persistent, re-armable portfolio alert. / Сохраняемый переактивируемый алерт."""

    __tablename__ = "risk_alerts"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_risk_alert_dedupe_key"),
        Index("ix_risk_alert_status_created", "status", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    exchange_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exchange_accounts.id"),
        nullable=False,
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32))
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(256), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="PENDING", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(128))


class DeliveryRecord(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("signal_id", "user_id", "stage", name="uq_delivery_signal_user_stage"),
        Index("ix_deliveries_user_status_created", "user_id", "status", "created_at"),
    )

    signal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("signals.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(String(128))


class ServiceHealthRecord(Base):
    __tablename__ = "service_health"

    component: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconnects: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lag_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
