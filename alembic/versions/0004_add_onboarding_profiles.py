"""add onboarding_profiles

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-26

회원가입 전 익명 상태에서 저장하는 온보딩 답변 테이블. 세션 쿠키 없이 스테이트리스
구조를 유지하기 위해, user_id는 처음엔 null이고 회원가입 시 SignupRequest.onboarding_id로
연결된다 (app/routers/auth.py signup, app/models/onboarding.py 참고).

서비스 테이블이라 alembic/env.py의 CORPUS_TABLES 제외 목록에는 넣지 않는다.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "onboarding_profiles",
        sa.Column("onboarding_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            unique=True,
            nullable=True,
        ),
        sa.Column("user_type", sa.String(50)),
        sa.Column("experience", sa.String(50)),
        sa.Column("purposes", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("fields", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("stage", sa.String(50)),
        sa.Column("venue", sa.String(100)),
        sa.Column("result_order", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("onboarding_profiles")
