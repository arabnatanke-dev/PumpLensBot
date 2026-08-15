"""Add EARLY radar persistence. / Добавляет хранение раннего радара.

Revision ID: 0003_early_radar
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from pumplens.storage.models import EarlyOutcomeRecord

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
    EarlyOutcomeRecord.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    EarlyOutcomeRecord.__table__.drop(bind=bind, checkfirst=True)
    columns = {column["name"] for column in inspect(bind).get_columns("signals")}
    if "early_liquidity_tier" in columns:
        op.drop_column("signals", "early_liquidity_tier")
    if "early_price" in columns:
        op.drop_column("signals", "early_price")
    if "early_at" in columns:
        op.drop_index("ix_signals_early_at", table_name="signals")
        op.drop_column("signals", "early_at")
