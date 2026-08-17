"""onboarding_profiles에서 아무 곳에서도 안 읽는 컬럼 3개를 지운다

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-16

purposes·result_order·stage는 온보딩 생성 시 쓰기만 하고, 백엔드 어디에서도
읽지 않는다(app/routers/onboarding.py의 대입문 세 줄이 유일한 참조였다). 원래
purposes → result_order로 결과 카드 순서를 정하려던 기능이었는데, 그 기능 자체가
폐기됐고(실제 리포트 화면 카드 이름과 안 맞았다), stage는 애초에 어디에도 안
쓰였다. 프론트도 이 질문들을 온보딩에서 뺀 지 오래라 항상 빈 값만 보내고 있었다.

읽는 곳이 없으니 지워도 잃을 게 없다. downgrade는 컬럼만 되살리고 값은 못
되살린다 — 지우는 시점에 이미 다 빈 값이었기 때문이다.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("onboarding_profiles", "purposes")
    op.drop_column("onboarding_profiles", "result_order")
    op.drop_column("onboarding_profiles", "stage")


def downgrade() -> None:
    op.add_column(
        "onboarding_profiles",
        sa.Column("stage", sa.String(50), nullable=True))
    op.add_column(
        "onboarding_profiles",
        sa.Column("result_order", postgresql.JSONB(), nullable=False, server_default="[]"))
    op.add_column(
        "onboarding_profiles",
        sa.Column("purposes", postgresql.JSONB(), nullable=False, server_default="[]"))
