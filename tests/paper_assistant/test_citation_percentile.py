"""인용 백분위 재계산 (paper_assistant/ingest/citation_percentile.py).

백분위는 **상대값**이라 한 논문만 고칠 수 없다 — 어떤 논문의 인용수가 바뀌면 그 해
다른 논문들의 백분위가 전부 조금씩 움직인다. 그래서 s2_enricher 가 citation_count 를
갱신한 뒤에는 그 해 전체를 다시 계산해야 한다.

여기서 지키려는 것:
  - **멱등** — 값이 이미 맞으면 한 행도 쓰지 않는다. 안 그러면 보강을 돌릴 때마다
    3만 행이 통째로 재작성되어 WAL 과 테이블 부풀림이 공짜로 쌓인다.
  - **인용수가 사라지면 백분위도 지운다** — 재보강으로 NULL 이 될 수 있다.
  - **보강 배치가 이 단계를 실제로 부른다** — 안 부르면 에러도 없이 랭킹이 옛
    인용도로 돈다.
"""
import inspect

import pytest

from paper_assistant.db.connection import cursor
from paper_assistant.ingest import citation_percentile


def _has_corpus() -> bool:
    try:
        with cursor() as cur:
            cur.execute("SELECT count(*) FROM papers")
            return cur.fetchone()[0] > 0
    except Exception:
        return False


needs_corpus = pytest.mark.skipif(
    not _has_corpus(), reason="논문 코퍼스가 필요하다 (restore_db.sh)")


def test_enrichment_batch_actually_calls_the_refresh():
    """DB 없이도 확인할 수 있는 가장 중요한 것 — 배치가 이 단계를 부르는가.

    이 호출이 빠지면 citation_count 만 갱신되고 백분위는 낡은 채로 남는데,
    **에러도 로그도 없어서** 아무도 모른다. 문서에 적어두는 것만으로는 부족해서
    자동화한 것이므로, 그 연결을 테스트로 고정한다.
    """
    from scripts import run_enrichment
    src = inspect.getsource(run_enrichment)
    assert "citation_percentile.refresh()" in src


@needs_corpus
def test_refresh_is_idempotent():
    """두 번째 호출은 한 행도 쓰지 않아야 한다.

    예전에 REAL 컬럼을 double 과 비교해 **항상 '다르다'** 로 판정되던 버그가 있었다.
    값은 맞는데 매번 3만 행을 재작성했다.
    """
    citation_percentile.refresh()                 # 상태를 맞춰 놓고
    cleared, updated = citation_percentile.refresh()
    assert (cleared, updated) == (0, 0)


@needs_corpus
def test_percentiles_stay_within_range_and_match_citation_counts():
    citation_percentile.refresh()
    with cursor() as cur:
        cur.execute("""
            SELECT
              count(*) FILTER (WHERE citation_count IS NOT NULL
                               AND citation_percentile IS NULL),
              count(*) FILTER (WHERE citation_count IS NULL
                               AND citation_percentile IS NOT NULL),
              count(*) FILTER (WHERE citation_percentile NOT BETWEEN 0 AND 1)
            FROM papers
        """)
        missing, orphaned, out_of_range = cur.fetchone()
    assert missing == 0, "인용수가 있는데 백분위가 없다"
    assert orphaned == 0, "인용수가 없는데 백분위가 남아 있다"
    assert out_of_range == 0, "백분위가 [0,1] 밖이다"


@needs_corpus
def test_percentile_is_computed_within_each_year():
    """연도별로 따로 계산돼야 한다 — 그러지 않으면 오래된 논문이 자동으로 상위가 된다.

    각 연도에 최댓값 1.0 이 존재하는지로 확인한다. 전체를 한 덩어리로 계산했다면
    1.0 은 코퍼스 전체에 하나뿐일 것이다.
    """
    citation_percentile.refresh()
    with cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM (
                SELECT year, max(citation_percentile) AS top
                FROM papers WHERE citation_percentile IS NOT NULL
                GROUP BY year
            ) t WHERE top > 0.999
        """)
        years_with_top = cur.fetchone()[0]
        cur.execute("SELECT count(DISTINCT year) FROM papers "
                    "WHERE citation_percentile IS NOT NULL")
        total_years = cur.fetchone()[0]
    assert years_with_top == total_years, "연도마다 최상위(1.0)가 있어야 한다"
