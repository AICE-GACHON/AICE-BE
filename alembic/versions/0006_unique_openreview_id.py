"""make users.openreview_id unique

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-29

리뷰에서 발견: openreview_id에 unique 제약이 없어서 두 계정이 같은 OpenReview
계정을 자처할 수 있었다. 제약을 걸기 전에 기존 행을 먼저 정리한다 — 특히 0004의
백필 플레이스홀더("MIGRATION_PLACEHOLDER")가 여러 행에 남아있으면 제약 추가가
그 자리에서 실패하므로, 각 중복 그룹에서 가장 먼저 만들어진 행만 원래 값을 두고
나머지는 user_id를 붙여 고유하게 만든다.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE users u
        SET openreview_id = u.openreview_id || '_' || u.user_id::text
        WHERE u.user_id IN (
            SELECT user_id FROM (
                SELECT user_id,
                       ROW_NUMBER() OVER (PARTITION BY openreview_id ORDER BY created_at) AS rn
                FROM users
            ) dup WHERE dup.rn > 1
        )
        """
    )
    op.create_unique_constraint("users_openreview_id_key", "users", ["openreview_id"])


def downgrade() -> None:
    op.drop_constraint("users_openreview_id_key", "users", type_="unique")
