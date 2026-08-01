"""add users.google_sub, users.openreview_id; users.password_hash nullable

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-26

Google 로그인 연동과 OpenReview ID 필수 입력을 위한 컬럼을 추가한다.

- google_sub: 이메일 가입 계정은 null. 나중에 같은 이메일로 구글 로그인하면 채워진다.
- openreview_id: 서비스 전체가 OpenReview 코퍼스 기반이라 필수 값으로 둔다. 기존 행은
  값이 없으므로, 일단 nullable로 추가해 플레이스홀더로 채운 뒤 NOT NULL로 잠근다
  (컬럼 추가와 제약을 한 번에 걸면 기존 행 때문에 실패한다).
- password_hash: Google 전용 계정은 비밀번호가 없어 nullable로 완화한다.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_PLACEHOLDER = "MIGRATION_PLACEHOLDER"


def upgrade() -> None:
    op.add_column("users", sa.Column("google_sub", sa.String(255), nullable=True))
    op.create_unique_constraint("users_google_sub_key", "users", ["google_sub"])

    op.add_column("users", sa.Column("openreview_id", sa.String(100), nullable=True))
    op.execute(
        sa.text("UPDATE users SET openreview_id = :placeholder WHERE openreview_id IS NULL")
        .bindparams(placeholder=_PLACEHOLDER)
    )
    op.alter_column("users", "openreview_id", nullable=False)

    op.alter_column("users", "password_hash", existing_type=sa.String(500), nullable=True)


def downgrade() -> None:
    op.alter_column("users", "password_hash", existing_type=sa.String(500), nullable=False)
    op.drop_column("users", "openreview_id")
    op.drop_constraint("users_google_sub_key", "users", type_="unique")
    op.drop_column("users", "google_sub")
