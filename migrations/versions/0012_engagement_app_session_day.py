"""engagement.app_session_day — one row per user per active day

System-of-record for app usage so the admin dashboard can report real
total/active-user counts instead of only users-who-favorited. cs-server records
each authenticated app session pseudonymized (HMAC, like engagement.favorite) via
POST /v1/sessions; the admin reads distinct-user windows from RDS. One row per
(user_pseudo, activity_date) is enough for total + DAU/WAU/MAU and is privacy-
minimal — no timestamps, no per-request event log. The PK + ON CONFLICT DO
NOTHING makes the daily ping idempotent; the date index serves the window counts.

Deploy does NOT run alembic (CI-only) — apply this manually on RDS before/with
the vibes_bot release, e.g. `docker exec vibes_bot-cs-server-1 alembic upgrade
head`. Without it, POST /v1/sessions 500s on the missing table.

Revision ID: 0012_engagement_app_session_day
Revises: 0011_widen_alembic_version
Create Date: 2026-06-18
"""
from alembic import op

revision = "0012_engagement_app_session_day"
down_revision = "0011_widen_alembic_version"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE IF NOT EXISTS engagement.app_session_day (
  user_pseudo   text NOT NULL,               -- HMAC(user_id); raw id never stored
  activity_date date NOT NULL,               -- America/Recife calendar day
  PRIMARY KEY (user_pseudo, activity_date)
);
CREATE INDEX IF NOT EXISTS ix_app_session_day_date
  ON engagement.app_session_day (activity_date);
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS engagement.app_session_day;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
