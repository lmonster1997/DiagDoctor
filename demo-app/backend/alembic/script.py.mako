"""Alembic migrations script template."""

revision = ${repr(up_revision) | trim}
down_revision = ${repr(down_revision) | trim}
branch_labels = ${repr(branch_labels) | trim}
depends_on = ${repr(depends_on) | trim}

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
