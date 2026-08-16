"""onboarding_profiles에 검색 우선순위 필드 추가, venue를 다중 선택으로 바꾼다

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-16

프론트 온보딩 2단계(유사성 판단 기준·최신성/인용도 우선순위)가 지금까지 값을
세션에만 들고 있었다 — 계정에 저장할 컬럼 자체가 없었기 때문이다. 이 값들은 아직
검색 로직(hybrid_search.py·_RERANK_SYSTEM)에 반영하지 않는다 — 지금은 저장만
한다. similarity_focus·recency_bias 둘 다 nullable이다: 값이 없다는 것 자체가
"균형있게"(프론트 기본값)와 같은 뜻이라, 별도 sentinel이 필요 없다.

venue도 프론트가 이미 다중 선택으로 바꿔뒀는데(개수 제한 없음) 여기가 아직
문자열 하나만 받고 있었다. purposes/fields/result_order와 같은 JSONB 리스트로
맞춘다. 기존 값이 있으면 배열 하나짜리로, 없으면 빈 배열로 옮긴다 — 되돌릴 때는
배열의 첫 원소만 남기고 나머지는 버린다(문자열 컬럼은 애초에 하나만 담을 수
있었으므로 완전한 역연산은 불가능하다).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "onboarding_profiles",
        sa.Column("similarity_focus", sa.String(50), nullable=True))
    op.add_column(
        "onboarding_profiles",
        sa.Column("recency_bias", sa.String(50), nullable=True))

    op.add_column(
        "onboarding_profiles",
        sa.Column("venue_list", postgresql.JSONB(), nullable=True))
    op.execute("""
        UPDATE onboarding_profiles
        SET venue_list = CASE
            WHEN venue IS NULL THEN '[]'::jsonb
            ELSE jsonb_build_array(venue)
        END
    """)
    op.alter_column("onboarding_profiles", "venue_list", nullable=False)
    op.drop_column("onboarding_profiles", "venue")
    op.alter_column("onboarding_profiles", "venue_list", new_column_name="venue")


def downgrade() -> None:
    op.add_column(
        "onboarding_profiles",
        sa.Column("venue_str", sa.String(100), nullable=True))
    op.execute("""
        UPDATE onboarding_profiles
        SET venue_str = venue ->> 0
        WHERE jsonb_array_length(venue) > 0
    """)
    op.drop_column("onboarding_profiles", "venue")
    op.alter_column("onboarding_profiles", "venue_str", new_column_name="venue")

    op.drop_column("onboarding_profiles", "recency_bias")
    op.drop_column("onboarding_profiles", "similarity_focus")
