"""Persist personal symbol monitors. / Сохраняет персональные мониторы монет.

Revision ID: 0009_personal_symbol_monitor
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0009_personal_symbol_monitor"
down_revision = "0008_stage_c_entry_analysis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy 0001 creates current metadata on fresh SQLite test databases.
    # Старый 0001 создаёт current metadata в свежих SQLite тестах.
    if inspect(op.get_bind()).has_table("monitored_scenarios"):
        return
    # Keep this schema frozen inside the migration. / Схема заморожена внутри миграции.
    op.create_table(
        "monitored_scenarios",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(8)),
        sa.Column("setup_type", sa.String(32)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False, server_default="1m"),
        sa.Column("trigger_level", sa.Numeric(38, 18)),
        sa.Column("retest_zone_low", sa.Numeric(38, 18)),
        sa.Column("retest_zone_high", sa.Numeric(38, 18)),
        sa.Column("invalidation_level", sa.Numeric(38, 18)),
        sa.Column("target1", sa.Numeric(38, 18)),
        sa.Column("target2", sa.Numeric(38, 18)),
        sa.Column("target3", sa.Numeric(38, 18)),
        sa.Column(
            "require_volume_confirmation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "require_pressure_confirmation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "require_oi_confirmation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("breakout_observed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("retest_observed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_evaluated_candle_open_time", sa.BigInteger()),
        sa.Column("latest_price", sa.Numeric(38, 18)),
        sa.Column("long_score", sa.Numeric(6, 2)),
        sa.Column("short_score", sa.Numeric(6, 2)),
        sa.Column("analysis_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("reason_code", sa.String(64)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_monitored_scenarios_active_user",
        "monitored_scenarios",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
        sqlite_where=sa.text("is_active"),
    )
    op.create_index(
        "ix_monitored_scenarios_symbol_status",
        "monitored_scenarios",
        ["symbol", "status"],
    )


def downgrade() -> None:
    if not inspect(op.get_bind()).has_table("monitored_scenarios"):
        return
    op.drop_index("ix_monitored_scenarios_symbol_status", table_name="monitored_scenarios")
    op.drop_index("uq_monitored_scenarios_active_user", table_name="monitored_scenarios")
    op.drop_table("monitored_scenarios")
