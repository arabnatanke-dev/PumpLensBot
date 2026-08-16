"""Separate Telegram reply-menu anchor. / Отделяет anchor постоянного меню.

Revision ID: 0007_telegram_reply_menu_anchor
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0007_telegram_reply_menu_anchor"
down_revision = "0006_full_portfolio"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("users")}
    if "telegram_menu_message_id" not in columns:
        op.add_column("users", sa.Column("telegram_menu_message_id", sa.BigInteger()))


def downgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("users")}
    if "telegram_menu_message_id" in columns:
        op.drop_column("users", "telegram_menu_message_id")
