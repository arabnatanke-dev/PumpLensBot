"""Persist one-shot position milestones. / Хранит одноразовые пороги позиции.

Revision ID: 0004_position_risk_state
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0004_position_risk_state"
down_revision = "0003_early_radar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "position_risk_states" in inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "position_risk_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "exchange_account_id",
            sa.Uuid(),
            sa.ForeignKey("exchange_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("max_profit_milestone", sa.Numeric(8, 2), nullable=False),
        sa.Column("max_loss_milestone", sa.Numeric(8, 2), nullable=False),
        sa.Column("last_roi_pct", sa.Numeric(10, 4), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "exchange_account_id",
            "symbol",
            "side",
            name="uq_position_risk_state_account_symbol_side",
        ),
    )


def downgrade() -> None:
    if "position_risk_states" in inspect(op.get_bind()).get_table_names():
        op.drop_table("position_risk_states")
