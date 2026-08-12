"""paper_body_diffs 추가 — 리비전 본문(PDF) diff 캐시

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-07

GET /api/papers/{id}/revisions/body-diff 는 pdf가 교체된 지점마다 전후 PDF를
실제로 내려받아 텍스트를 뽑고 diff한다. 심사가 끝난 논문은 리비전도 본문도 더
안 바뀌므로 결과를 통째로 JSONB에 넣어두고 재사용한다 (paper_stories, alembic
0009와 같은 판단·같은 모양 — 다만 LLM이 전혀 관여하지 않는 기능이라 used_llm
컬럼은 두지 않는다).

코퍼스 테이블(papers)을 참조하므로 0008·0009와 같은 방어를 건다:
- to_regclass 가드 — 서비스 테이블만 있는 DB(코퍼스 없이 백엔드만 띄운 환경)에서
  upgrade 를 돌려도 그냥 넘어간다. 이 경우 body-diff 엔드포인트는 캐시 없이 동작한다.
- IF NOT EXISTS — 새 컨테이너는 scripts/init_db.sql 이 이미 만들어 둔다.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.papers') IS NOT NULL THEN
                CREATE TABLE IF NOT EXISTS paper_body_diffs (
                    paper_id BIGINT PRIMARY KEY
                             REFERENCES papers(id) ON DELETE CASCADE,
                    payload  JSONB       NOT NULL,
                    built_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS paper_body_diffs")
