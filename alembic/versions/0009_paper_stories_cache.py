"""paper_stories 추가 — 심사 서사(get_paper_story) 캐시

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-02

GET /api/papers/{id}/story 는 응답 하나를 만들려고 OpenReview API를 두 번(포럼
리플라이 + 수정 이력) 타고, LLM을 켜면 Sonnet 호출까지 붙는다. 유사 논문 목록에서
논문을 눌러볼 때마다 이걸 다시 하면 화면이 느리고 비용도 반복된다.

심사가 끝난 논문은 리뷰도 수정 이력도 더 바뀌지 않으므로 결과를 통째로 JSONB에
넣어두고 재사용한다. 스키마를 컬럼으로 펼치지 않는 이유는 이 payload가 프론트에
그대로 나가는 응답 본문이고, PaperStory 모델이 바뀔 때 마이그레이션을 다시 쓰고
싶지 않아서다 — 모델이 바뀌면 캐시를 버리면 된다.

used_llm 을 따로 둔 건 스텁으로 만든 캐시가 LLM 켠 요청에 그대로 나가는 걸 막기
위해서다. 컬럼으로 빼두면 payload를 파싱하지 않고 WHERE 에서 거를 수 있다.

코퍼스 테이블(papers)을 참조하므로 0008과 같은 방어를 건다:
- to_regclass 가드 — 서비스 테이블만 있는 DB(코퍼스 없이 백엔드만 띄운 환경)에서
  upgrade 를 돌려도 그냥 넘어간다. 이 경우 story 엔드포인트는 캐시 없이 동작한다.
- IF NOT EXISTS — 새 컨테이너는 scripts/init_db.sql 이 이미 만들어 둔다.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.papers') IS NOT NULL THEN
                CREATE TABLE IF NOT EXISTS paper_stories (
                    paper_id BIGINT PRIMARY KEY
                             REFERENCES papers(id) ON DELETE CASCADE,
                    payload  JSONB       NOT NULL,
                    used_llm BOOLEAN     NOT NULL DEFAULT false,
                    built_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS paper_stories")
