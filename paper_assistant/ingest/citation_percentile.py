"""papers.citation_percentile 재계산.

**왜 따로 재계산해야 하나**: 백분위는 상대값이라 한 논문만 고칠 수 없다. 어떤
논문의 인용수가 5 → 200으로 늘면 그 해 **다른 논문들의 백분위가 전부 조금씩
내려간다.** 그래서 `s2_enricher` 가 `citation_count` 를 갱신한 뒤에는 그 해 전체를
다시 계산해야 하고, `UPDATE ... WHERE id = ?` 같은 개별 갱신으로는 유지가 안 된다.

**이 파일이 단일 소스다.** 예전에는 같은 SQL 이 `scripts/refresh_citation_percentile.sql`
에도 있었는데, 두 벌을 두면 한쪽만 고쳤을 때 조용히 갈라진다 — 이 저장소에서
그런 종류의 버그를 이미 여러 번 겪었다. 수동 실행도 이 모듈로 한다:

    python -m paper_assistant.ingest.citation_percentile

자동 실행은 `scripts/run_enrichment.py` 의 마지막 단계다. 설계 근거(왜 절대
인용수가 아니라 백분위인지, 왜 cume_dist 인지, 왜 NULL 을 채우지 않는지)는
alembic 0010 과 docs/랭킹_가중치_설계.md 참고.
"""
import logging

from paper_assistant.db.connection import cursor

log = logging.getLogger(__name__)

# 인용수가 사라진 논문은 백분위도 지운다 (재보강으로 NULL 이 될 수 있다).
_CLEAR_ORPHANS = """
    UPDATE papers
    SET citation_percentile = NULL
    WHERE citation_count IS NULL AND citation_percentile IS NOT NULL
"""

# ⚠️ 비교할 때 s.pct 를 REAL 로 캐스팅해야 한다. citation_percentile 은 REAL(float4)
# 인데 cume_dist() 는 double precision 을 낸다. 캐스팅 없이 IS DISTINCT FROM 을 쓰면
# REAL 이 double 로 승격되면서 저장 시 잃은 반올림 오차(~3e-08) 때문에 **항상
# "다르다"** 로 판정되어, 값이 이미 맞는데도 매번 3만 행을 통째로 다시 쓴다
# (실측으로 확인). 결과는 같지만 WAL 과 테이블 부풀림이 공짜로 쌓인다.
_RECOMPUTE = """
    UPDATE papers p
    SET citation_percentile = s.pct
    FROM (
        SELECT id,
               cume_dist() OVER (PARTITION BY year ORDER BY citation_count) AS pct
        FROM papers
        WHERE citation_count IS NOT NULL
    ) s
    WHERE p.id = s.id
      AND (p.citation_percentile IS DISTINCT FROM s.pct::real)
"""


def refresh() -> tuple[int, int]:
    """백분위를 다시 계산한다. (지운 행 수, 갱신한 행 수) 반환.

    멱등하다 — 값이 이미 맞으면 한 행도 쓰지 않는다. 보강 배치를 여러 번 돌려도
    안전하고, 실제로 (0, 0) 이 나오는 것이 정상 상태다.
    """
    with cursor(commit=True) as cur:
        cur.execute(_CLEAR_ORPHANS)
        cleared = cur.rowcount
        cur.execute(_RECOMPUTE)
        updated = cur.rowcount
    log.info("인용 백분위 재계산 — 정리 %d행 / 갱신 %d행", cleared, updated)
    return cleared, updated


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    refresh()
