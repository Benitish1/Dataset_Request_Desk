"""initial schema"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def ts():
    return sa.DateTime(timezone=True)


now = sa.text("now()")


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("organisation", sa.String(120)),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("password_hash", sa.String(100), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.CheckConstraint("role in ('client','operator','admin')", name="ck_users_role"),
    )
    op.create_table(
        "episodes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("episode_id", sa.String(40), nullable=False, unique=True),
        sa.Column("robot_id", sa.String(40), nullable=False),
        sa.Column("task_name", sa.String(120), nullable=False),
        sa.Column("recorded_at", ts(), nullable=False),
        sa.Column("duration_seconds", sa.Integer, nullable=False),
        sa.Column("operator_name", sa.String(80), nullable=False),
        sa.Column("quality", sa.String(10), nullable=False),
        sa.CheckConstraint("quality in ('good','usable','bad')", name="ck_episodes_quality"),
        sa.CheckConstraint("duration_seconds > 0", name="ck_episodes_duration"),
    )
    op.create_index("ix_episodes_recorded_robot", "episodes", ["recorded_at", "robot_id"])
    op.create_index("ix_episodes_task_quality", "episodes", ["task_name", "quality"])
    op.create_table(
        "requests",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("task_name", sa.String(120), nullable=False),
        sa.Column("episodes_requested", sa.Integer, nullable=False),
        sa.Column("deadline", sa.Date, nullable=False),
        sa.Column("notes", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(12), nullable=False, server_default="submitted", index=True),
        sa.Column("created_at", ts(), nullable=False, server_default=now),
        sa.CheckConstraint(
            "status in ('submitted','in_progress','delivered','accepted','rejected')", name="ck_requests_status"
        ),
        sa.CheckConstraint("episodes_requested > 0", name="ck_requests_count"),
    )
    op.create_table(
        "status_history",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("request_id", sa.Integer, sa.ForeignKey("requests.id"), nullable=False, index=True),
        sa.Column("from_status", sa.String(12)),
        sa.Column("to_status", sa.String(12), nullable=False),
        sa.Column("actor_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("at", ts(), nullable=False, server_default=now),
    )
    op.create_table(
        "assignments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("request_id", sa.Integer, sa.ForeignKey("requests.id"), nullable=False, index=True),
        # UNIQUE = an episode belongs to at most one request, enforced by the database itself
        sa.Column("episode_pk", sa.Integer, sa.ForeignKey("episodes.id"), nullable=False, unique=True),
        sa.Column("assigned_by", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("assigned_at", ts(), nullable=False, server_default=now),
    )


def downgrade():
    for t in ("assignments", "status_history", "requests", "episodes", "users"):
        op.drop_table(t)
