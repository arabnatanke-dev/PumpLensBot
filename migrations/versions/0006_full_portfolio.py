"""Add full read-only portfolio sources. / Добавляет полные read-only источники.

Revision ID: 0006_full_portfolio
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0006_full_portfolio"
down_revision = "0005_telegram_clean_ui"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    snapshot_columns = {
        column["name"] for column in inspect(bind).get_columns("portfolio_snapshots")
    }
    with op.batch_alter_table("portfolio_snapshots") as batch:
        if "earn_value" not in snapshot_columns:
            batch.add_column(sa.Column("earn_value", sa.Numeric(38, 18)))
        if "funding_value" not in snapshot_columns:
            batch.add_column(sa.Column("funding_value", sa.Numeric(38, 18)))
        if "source_status_json" not in snapshot_columns:
            batch.add_column(
                sa.Column(
                    "source_status_json",
                    sa.JSON(),
                    server_default=sa.text("'{}'"),
                    nullable=False,
                )
            )

    tables = set(inspect(bind).get_table_names())
    if "earn_holdings" not in tables:
        op.create_table(
            "earn_holdings",
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
            sa.Column("asset", sa.String(32), nullable=False),
            sa.Column("product_type", sa.String(16), nullable=False),
            sa.Column("product_id", sa.String(128)),
            sa.Column("amount", sa.Numeric(38, 18), nullable=False),
            sa.Column("value_usdt", sa.Numeric(38, 18)),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "exchange_account_id",
                "product_type",
                "product_id",
                "asset",
                name="uq_earn_account_product_asset",
            ),
        )
        op.create_index(
            "ix_earn_account_asset",
            "earn_holdings",
            ["exchange_account_id", "asset"],
        )
    if "funding_holdings" not in tables:
        op.create_table(
            "funding_holdings",
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
            sa.Column("asset", sa.String(32), nullable=False),
            sa.Column("amount", sa.Numeric(38, 18), nullable=False),
            sa.Column("value_usdt", sa.Numeric(38, 18)),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "exchange_account_id",
                "asset",
                name="uq_funding_account_asset",
            ),
        )
        op.create_index(
            "ix_funding_account_asset",
            "funding_holdings",
            ["exchange_account_id", "asset"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "funding_holdings" in tables:
        op.drop_index("ix_funding_account_asset", table_name="funding_holdings")
        op.drop_table("funding_holdings")
    if "earn_holdings" in tables:
        op.drop_index("ix_earn_account_asset", table_name="earn_holdings")
        op.drop_table("earn_holdings")

    snapshot_columns = {
        column["name"] for column in inspect(bind).get_columns("portfolio_snapshots")
    }
    with op.batch_alter_table("portfolio_snapshots") as batch:
        if "source_status_json" in snapshot_columns:
            batch.drop_column("source_status_json")
        if "funding_value" in snapshot_columns:
            batch.drop_column("funding_value")
        if "earn_value" in snapshot_columns:
            batch.drop_column("earn_value")
