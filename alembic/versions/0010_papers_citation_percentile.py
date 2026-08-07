"""papers.citation_percentile 추가 + 백필 (랭킹 가중치용)

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-05

검색 랭킹에 인용수를 반영하려면 **절대 인용수를 그대로 쓰면 안 된다.** 코퍼스
실측상 평균 인용수가 2020년 200.8 → 2025년 26.9로, 오래된 논문일수록 인용을
쌓을 시간이 많았을 뿐인데 자동으로 상위를 차지한다. "최신 논문을 우선"이라는
요구사항과 정면으로 충돌한다. 분포도 극단적이라(중앙값 18, 최대 67,188) 선형으로
쓰면 논문 한 편이 순위를 지배한다.

그래서 **같은 연도 안에서의 인용 백분위**를 미리 계산해 둔다. "2025년 논문 중
상위 10%"가 "2020년 논문 중 상위 10%"와 공정하게 겨루고, 백분위라 치우친 분포도
자동으로 눌린다. 설계 근거 전체는 docs/랭킹_가중치_설계.md 참고.

**cume_dist를 쓰는 이유**: percent_rank는 그 해 최하위 그룹에 전부 0.0을 준다.
인용 0회 논문이 20%를 차지하는 해라면 그 20% 전원이 0점이 되어 과도하게 벌받는다.
cume_dist는 "나 이하가 전체의 몇 %인가"라 그 경우 0.20을 준다 — 백분위의 통상적
의미에 더 가깝다. 0.5가 곧 그 해의 중앙값 위치다.

**NULL은 채우지 않는다.** citation_count가 없는 논문(코퍼스의 30.5%)은 백분위도
NULL로 남긴다. 검색 시점에 COALESCE로 중립값 0.5를 준다 — NULL은 "인용이 0회"가
아니라 "S2 보강이 닿지 않았다"는 뜻이라, 상도 벌도 주지 않는 게 맞다. 실제로
결측률이 연도별로 고르지 않아(2022년 80.0% vs 2025년 58.0%) 0으로 채우면 하필
가장 밀어올려야 할 2025년이 손해를 본다.

0008과 같은 방어 패턴을 쓴다 — 코퍼스 테이블은 alembic이 아니라 init_db.sql이
만들므로, 서비스 테이블만 있는 DB에 돌려도 그냥 넘어가야 한다.

**주의**: 이 백분위는 s2_enricher가 citation_count를 갱신하면 낡는다. 한 논문의
인용수가 바뀌면 그 해 전체의 백분위가 흔들리므로 개별 UPDATE로는 유지할 수 없다.
보강 배치 후 scripts/refresh_citation_percentile.sql 을 한 번 돌릴 것.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.papers') IS NOT NULL THEN
                ALTER TABLE papers
                    ADD COLUMN IF NOT EXISTS citation_percentile REAL;

                UPDATE papers p
                SET citation_percentile = s.pct
                FROM (
                    SELECT id,
                           cume_dist() OVER (PARTITION BY year
                                             ORDER BY citation_count) AS pct
                    FROM papers
                    WHERE citation_count IS NOT NULL
                ) s
                WHERE p.id = s.id;
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
                ALTER TABLE papers DROP COLUMN IF EXISTS citation_percentile;
            END IF;
        END $$
        """
    )
