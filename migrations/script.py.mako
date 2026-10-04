"""${message}"""
from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy
${imports if imports else ""}
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}

def upgrade():
    """Apply the generated schema changes."""
    ${upgrades if upgrades else "pass"}

def downgrade():
    """Reverse the generated schema changes."""
    ${downgrades if downgrades else "pass"}
