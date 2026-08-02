"""papers.citation_count 추가 (init_db.sql 과의 드리프트 복구)

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-02

코퍼스 테이블(papers 등)은 alembic이 아니라 scripts/init_db.sql 이 컨테이너 최초
기동 때 한 번 만든다. 그래서 init_db.sql 에 컬럼을 추가해도 **이미 떠 있는 DB는
따라오지 않는다** — 볼륨을 지우고 다시 만들지 않는 한 영원히 어긋난 채로 남는다.

지금이 그 상태다. init_db.sql 에는 `citation_count INT` 가 선언돼 있지만 운영
DB의 papers 에는 그 컬럼이 없어서, s2_enricher._apply_paper 의
`UPDATE papers SET ... citation_count = %s ...` 가 UndefinedColumn 으로 즉시
실패한다(= arXiv/S2 보강을 아직 한 번도 돌리지 못한 이유).

두 갈래 모두에서 안전하도록 방어적으로 쓴다:
- `IF NOT EXISTS` — 새로 만든 컨테이너는 init_db.sql 이 이미 컬럼을 만들어 둔다.
- `to_regclass` 가드 — 서비스 테이블만 있는 DB(코퍼스 없이 백엔드만 띄운 환경)에
  대고 upgrade 를 돌려도 그냥 넘어간다.

값 자체는 채우지 않는다. NULL 로 두고 s2_enricher 가 보강 시점에 스냅샷을 넣는다.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.papers') IS NOT NULL THEN
                ALTER TABLE papers ADD COLUMN IF NOT EXISTS citation_count INT;
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.papers') IS NOT NULL THEN
                ALTER TABLE papers DROP COLUMN IF EXISTS citation_count;
            END IF;
        END $$
        """
    )
