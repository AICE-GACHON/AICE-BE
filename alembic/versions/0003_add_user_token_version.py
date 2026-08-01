"""add users.token_version

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-26

로그아웃/refresh_token 폐기를 위한 버전 카운터. JWT는 상태가 없어 개별 토큰을
블랙리스트 없이 폐기할 수 없으므로, 로그아웃 시 이 값을 올려서 그 이전에 발급된
refresh_token(더 낮은 ver를 담고 있음)을 전부 무효화한다 (app/core/security.py 참고).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("users", "token_version")
