"""Add persistent portfolio risk alerts. / Добавляет постоянные риск-алерты.

Revision ID: 0002_portfolio_risk_alerts
"""

from alembic import op

from pumplens.storage.models import RiskAlertRecord

revision = "0002_portfolio_risk_alerts"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    RiskAlertRecord.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    RiskAlertRecord.__table__.drop(bind=op.get_bind(), checkfirst=True)
