"""Persist Telegram panel and signal cleanup. / Хранит панель и очистку сигналов.

Revision ID: 0005_telegram_clean_ui
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0005_telegram_clean_ui"
down_revision = "0004_position_risk_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    user_columns = {column["name"] for column in inspect(bind).get_columns("users")}
    if "telegram_panel_message_id" not in user_columns:
        op.add_column("users", sa.Column("telegram_panel_message_id", sa.BigInteger()))

    delivery_columns = {
        column["name"] for column in inspect(bind).get_columns("deliveries")
    }
    if "delete_after" not in delivery_columns:
        op.add_column(
            "deliveries",
            sa.Column("delete_after", sa.DateTime(timezone=True)),
        )
    if "deleted_at" not in delivery_columns:
        op.add_column(
            "deliveries",
            sa.Column("deleted_at", sa.DateTime(timezone=True)),
        )
    if "cleanup_error" not in delivery_columns:
        op.add_column("deliveries", sa.Column("cleanup_error", sa.String(128)))

    indexes = {index["name"] for index in inspect(bind).get_indexes("deliveries")}
    if "ix_deliveries_cleanup_due" not in indexes:
        op.create_index(
            "ix_deliveries_cleanup_due",
            "deliveries",
            ["delete_after", "deleted_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {index["name"] for index in inspect(bind).get_indexes("deliveries")}
    if "ix_deliveries_cleanup_due" in indexes:
        op.drop_index("ix_deliveries_cleanup_due", table_name="deliveries")

    delivery_columns = {
        column["name"] for column in inspect(bind).get_columns("deliveries")
    }
    if "cleanup_error" in delivery_columns:
        op.drop_column("deliveries", "cleanup_error")
    if "deleted_at" in delivery_columns:
        op.drop_column("deliveries", "deleted_at")
    if "delete_after" in delivery_columns:
        op.drop_column("deliveries", "delete_after")

    user_columns = {column["name"] for column in inspect(bind).get_columns("users")}
    if "telegram_panel_message_id" in user_columns:
        op.drop_column("users", "telegram_panel_message_id")
