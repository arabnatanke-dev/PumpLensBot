"""Add EARLY radar persistence. / Добавляет хранение раннего радара.

Revision ID: 0003_early_radar
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0003_early_radar"
down_revision = "0002_portfolio_risk_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("signals")}
    if "early_at" not in columns:
        op.add_column("signals", sa.Column("early_at", sa.DateTime(timezone=True)))
        op.create_index("ix_signals_early_at", "signals", ["early_at"])
    if "early_price" not in columns:
        op.add_column("signals", sa.Column("early_price", sa.Numeric(38, 18)))
    if "early_liquidity_tier" not in columns:
        op.add_column("signals", sa.Column("early_liquidity_tier", sa.String(24)))
    if "early_outcomes" not in inspect(bind).get_table_names():
        op.create_table(
            "early_outcomes",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column(
                "signal_id",
                sa.Uuid(),
                sa.ForeignKey("signals.id"),
                nullable=False,
            ),
            sa.Column("price_15s", sa.Numeric(38, 18)),
            sa.Column("price_30s", sa.Numeric(38, 18)),
            sa.Column("price_1m", sa.Numeric(38, 18)),
            sa.Column("price_3m", sa.Numeric(38, 18)),
            sa.Column("price_5m", sa.Numeric(38, 18)),
            sa.Column("mfe", sa.Numeric(8, 4)),
            sa.Column("mae", sa.Numeric(8, 4)),
            sa.Column("evaluated_at", sa.DateTime(timezone=True)),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "signal_id",
                name="uq_early_outcomes_signal_id",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "early_outcomes" in inspect(bind).get_table_names():
        op.drop_table("early_outcomes")
    columns = {column["name"] for column in inspect(bind).get_columns("signals")}
    if "early_liquidity_tier" in columns:
        op.drop_column("signals", "early_liquidity_tier")
    if "early_price" in columns:
        op.drop_column("signals", "early_price")
    if "early_at" in columns:
        op.drop_index("ix_signals_early_at", table_name="signals")
        op.drop_column("signals", "early_at")
