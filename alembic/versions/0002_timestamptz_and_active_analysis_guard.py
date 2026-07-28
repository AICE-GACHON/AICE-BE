"""timestamptz + 진행 중 분석 중복 방지

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-26

두 가지를 고칩니다.

1. **모든 시각 컬럼을 TIMESTAMPTZ로.**
   `created_at`은 `server_default=now()`(DB 서버 시각)이고 `completed_at`은 앱이
   만든 UTC 값이었습니다. 컨테이너가 UTC라 지금은 우연히 맞지만, 타임존이 다른
   환경에 배포하면 `completed_at < created_at`이 나옵니다. 기존 값은 UTC로 간주해
   변환합니다(`AT TIME ZONE 'UTC'`).

2. **한 초안에 진행 중인 분석은 하나만** (부분 유니크 인덱스).
   기존에는 애플리케이션이 "진행 중인 게 있나" 조회하고 없으면 INSERT 했는데,
   동시 요청 두 건이 같은 순간에 조회하면 둘 다 통과해 분석이 두 번 돕니다
   (SPECTER2 로드가 두 벌). DB에서 막고, 라우터는 IntegrityError를 받으면
   이미 만들어진 행을 돌려줍니다.

   ⚠️ 조건의 상태 목록은 app/models/analysis.py의 IN_PROGRESS와 같아야 합니다.

3. submissions_user 인덱스에 created_at을 추가합니다 (목록이 최신순 정렬이라).
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_TZ_COLUMNS = [
    ("users", "created_at"),
    ("users", "updated_at"),
    ("submissions", "created_at"),
    ("review_predictions", "created_at"),
    ("review_predictions", "completed_at"),
    ("similar_paper_matches", "created_at"),
]

ACTIVE_ANALYSIS_INDEX = "review_predictions_one_active"


def upgrade() -> None:
    for table, column in _TZ_COLUMNS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TIMESTAMPTZ "
            f"USING {column} AT TIME ZONE 'UTC'"
        )

    # 진행 중인 분석이 여러 개 남아 있으면 인덱스를 만들 수 없다. 가장 최근 것만
    # 남기고 나머지는 failed로 정리한다.
    op.execute(
        """
        UPDATE review_predictions p
           SET status = 'failed',
               error = '중복 실행되어 정리되었습니다 (마이그레이션 0002).',
               completed_at = now()
         WHERE p.status IN ('pending', 'running')
           AND p.prediction_id <> (
                SELECT q.prediction_id FROM review_predictions q
                 WHERE q.submission_id = p.submission_id
                   AND q.status IN ('pending', 'running')
                 ORDER BY q.created_at DESC
                 LIMIT 1)
        """
    )
    op.execute(
        f"CREATE UNIQUE INDEX {ACTIVE_ANALYSIS_INDEX} "
        f"ON review_predictions (submission_id) "
        f"WHERE status IN ('pending', 'running')"
    )

    op.drop_index("submissions_user", table_name="submissions")
    op.create_index("submissions_user", "submissions", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("submissions_user", table_name="submissions")
    op.create_index("submissions_user", "submissions", ["user_id"])

    op.execute(f"DROP INDEX IF EXISTS {ACTIVE_ANALYSIS_INDEX}")

    for table, column in _TZ_COLUMNS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TIMESTAMP "
            f"USING {column} AT TIME ZONE 'UTC'"
        )
