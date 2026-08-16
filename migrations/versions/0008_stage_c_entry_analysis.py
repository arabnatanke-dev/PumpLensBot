"""Persist Stage C entry analysis. / Сохраняет анализ Stage C.

Revision ID: 0008_stage_c_entry_analysis
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0008_stage_c_entry_analysis"
down_revision = "0007_telegram_reply_menu_anchor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    signal_columns = {column["name"] for column in inspect(bind).get_columns("signals")}
    if "entry_quality" not in signal_columns:
        op.add_column("signals", sa.Column("entry_quality", sa.Numeric(6, 2)))
    if "final_decision" not in signal_columns:
        op.add_column("signals", sa.Column("final_decision", sa.String(48)))

    feature_columns = {
        column["name"] for column in inspect(bind).get_columns("signal_features")
    }
    if "stage_c_json" not in feature_columns:
        op.add_column("signal_features", sa.Column("stage_c_json", sa.JSON()))
    if "entry_quality" not in feature_columns:
        op.add_column("signal_features", sa.Column("entry_quality", sa.Numeric(6, 2)))
    if "final_decision" not in feature_columns:
        op.add_column("signal_features", sa.Column("final_decision", sa.String(48)))
    if "reason_codes_json" not in feature_columns:
        op.add_column(
            "signal_features",
            sa.Column(
                "reason_codes_json",
                sa.JSON(),
                server_default=sa.text("'[]'"),
                nullable=False,
            ),
        )

    outcome_columns = {
        column["name"] for column in inspect(bind).get_columns("signal_outcomes")
    }
    if "last_sampled_at" not in outcome_columns:
        op.add_column(
            "signal_outcomes",
            sa.Column("last_sampled_at", sa.DateTime(timezone=True)),
        )


def downgrade() -> None:
    bind = op.get_bind()
    outcome_columns = {
        column["name"] for column in inspect(bind).get_columns("signal_outcomes")
    }
    if "last_sampled_at" in outcome_columns:
        op.drop_column("signal_outcomes", "last_sampled_at")

    feature_columns = {
        column["name"] for column in inspect(bind).get_columns("signal_features")
    }
    for column in (
        "reason_codes_json",
        "final_decision",
        "entry_quality",
        "stage_c_json",
    ):
        if column in feature_columns:
            op.drop_column("signal_features", column)

    signal_columns = {column["name"] for column in inspect(bind).get_columns("signals")}
    for column in ("final_decision", "entry_quality"):
        if column in signal_columns:
            op.drop_column("signals", column)
