"""Initial PumpLens schema. / Начальная схема PumpLens.

Revision ID: 0001_initial
"""

from alembic import op

from pumplens.storage.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Metadata is centralized in typed models to prevent migration/model drift.
    # Metadata централизованы в моделях, чтобы миграция и код не расходились.
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
