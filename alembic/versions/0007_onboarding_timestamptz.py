"""onboarding_profiles 시각 컬럼을 TIMESTAMPTZ로

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-02

0002가 서비스 테이블의 시각 컬럼을 전부 TIMESTAMPTZ로 옮겼는데, 그 뒤에 추가된
0005의 onboarding_profiles만 TIMESTAMP(타임존 없음)로 남아 있었습니다. 지금은
컨테이너가 UTC라 드러나지 않지만, 타임존이 다른 환경에 배포하면 0002가 고쳤던
것과 같은 문제(앱이 만든 UTC 값과 DB 서버 시각이 어긋남)가 재발합니다.

기존 값은 0002와 동일하게 UTC로 간주해 변환합니다(`AT TIME ZONE 'UTC'`).
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_TZ_COLUMNS = [
    ("onboarding_profiles", "created_at"),
    ("onboarding_profiles", "updated_at"),
]


def upgrade() -> None:
    for table, column in _TZ_COLUMNS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TIMESTAMPTZ "
            f"USING {column} AT TIME ZONE 'UTC'"
        )


def downgrade() -> None:
    for table, column in _TZ_COLUMNS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TIMESTAMP "
            f"USING {column} AT TIME ZONE 'UTC'"
        )
