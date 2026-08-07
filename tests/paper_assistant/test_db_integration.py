"""DB 통합 테스트. 컨테이너가 떠 있어야 실행된다 (없으면 자동 skip).

    docker compose up -d
    pytest tests/paper_assistant/test_db_integration.py
"""
import pytest

psycopg = pytest.importorskip("psycopg")

from paper_assistant import config
from paper_assistant.db.connection import cursor
from paper_assistant.ingest.normalize import NormalizedPaper, NormalizedReview


def _db_available() -> bool:
    try:
        with psycopg.connect(config.DATABASE_URL, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(), reason="Postgres 컨테이너 미기동 (docker compose up -d)")


@pytest.fixture
def sample_paper():
    return NormalizedPaper(
        openreview_id="test_paper_1", forum_id="test_paper_1",
        title="Graph Neural Networks for Molecular Property Prediction",
        abstract="We propose a graph neural network architecture that predicts "
                 "molecular properties using message passing over atom graphs, "
                 "evaluated on the QM9 benchmark dataset.",
        keywords=["graph neural networks", "molecules"],
        authors=["Test Author"], author_ids=["~Test_Author1"],
        venue="ICLR 2024", year=2024, decision="accept-poster",
        primary_area="applications",
        reviews=[NormalizedReview(
            openreview_id="test_review_1", rating=6.0, rating_raw="6: marginally above",
            confidence=4.0, summary="A GNN paper.", strengths="Clear.",
            weaknesses="Only evaluated on QM9; no larger benchmarks.",
            questions="Why not MD17?", needs_llm_split=False)],
        meta_review="Reviewers leaned positive.")


@pytest.fixture(autouse=True)
def cleanup():
    yield
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM papers WHERE openreview_id LIKE 'test_%'")
        cur.execute("DELETE FROM authors WHERE openreview_id LIKE '~Test_%'")


def test_schema_has_expected_tables():
    with cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'")
        tables = {row[0] for row in cur.fetchall()}
    assert {"papers", "reviews", "review_points", "authors", "paper_authors",
            "citations", "submission_links", "ingest_status"} <= tables


def test_pgvector_extension_is_installed():
    with cursor() as cur:
        cur.execute("SELECT extname FROM pg_extension")
        exts = {row[0] for row in cur.fetchall()}
    assert "vector" in exts
    assert "pg_trgm" in exts  # 재투고 제목 매칭에 필요


def test_load_and_reload_is_idempotent(sample_paper):
    """수집을 여러 번 돌려도 중복이 생기지 않아야 한다 (전부 upsert)."""
    from paper_assistant.db.load import load_papers

    load_papers([sample_paper])
    load_papers([sample_paper])  # 재실행

    with cursor() as cur:
        cur.execute("SELECT count(*) FROM papers WHERE openreview_id = 'test_paper_1'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM reviews WHERE openreview_id = 'test_review_1'")
        assert cur.fetchone()[0] == 1


def test_tsvector_is_generated_automatically(sample_paper):
    from paper_assistant.db.load import load_papers

    load_papers([sample_paper])
    with cursor() as cur:
        cur.execute("SELECT tsv IS NOT NULL FROM papers WHERE openreview_id = 'test_paper_1'")
        assert cur.fetchone()[0] is True


def test_fulltext_or_matching_beats_and_matching(sample_paper):
    """긴 초록을 쿼리로 넣었을 때 FTS가 결과를 내야 한다.

    회귀 방지: plainto_tsquery는 모든 단어를 AND로 묶어 200편 중 1편만
    매칭시켰다. OR 결합으로 바꾼 뒤에야 FTS가 실제로 순위에 기여한다.
    """
    from paper_assistant.db.load import load_papers
    from paper_assistant.retrieval.hybrid_search import _fulltext_search

    load_papers([sample_paper])
    long_query = (
        "We present a deep learning method for molecular property prediction "
        "using message passing neural networks on graphs of atoms and bonds, "
        "with experiments on quantum chemistry benchmarks and ablation studies.")

    with cursor() as cur:
        hits = _fulltext_search(cur, long_query, limit=10)
    assert hits, "긴 쿼리에서 FTS가 아무것도 찾지 못했다 (AND 결합 회귀)"


def test_author_linkage(sample_paper):
    from paper_assistant.db.load import load_papers

    load_papers([sample_paper])
    with cursor() as cur:
        cur.execute("""
            SELECT a.name FROM authors a
            JOIN paper_authors pa ON pa.author_id = a.id
            JOIN papers p ON p.id = pa.paper_id
            WHERE p.openreview_id = 'test_paper_1'
        """)
        assert [r[0] for r in cur.fetchall()] == ["Test Author"]


def test_fulltext_survives_urls_in_the_query(sample_paper):
    """초록에 URL이 있으면 검색이 통째로 죽던 회귀.

    to_tsvector는 URL을 lexeme으로 그대로 뱉는다(`github.com/a/b](https://…).`).
    괄호·콜론이 섞여 있어 to_tsquery 파서가 SyntaxError를 냈고, 그 결과 그런
    초록을 올린 사용자는 분석이 실패했다. lexeme을 quote_literal로 감싸 해결.
    """
    from paper_assistant.retrieval.hybrid_search import _fulltext_search

    query = ("See our code at https://github.com/foo/bar](https://github.com/foo/bar). "
             "We propose a graph neural network for molecular property prediction.")
    with cursor() as cur:
        hits = _fulltext_search(cur, query, limit=5)   # 예외가 나지 않아야 한다
    assert isinstance(hits, list)


def test_fulltext_still_matches_normal_abstracts(sample_paper):
    """인용을 씌운 뒤에도 평범한 초록이 정상적으로 걸려야 한다.

    표본을 **직접 적재한 뒤에** 검색한다. 예전에는 적재 없이 검색만 했는데, 그건
    개발 DB에 이미 들어 있는 코퍼스 43,515편에 기대는 것이었다 — 코퍼스가 없는
    빈 DB(=CI)에서는 매칭이 0건이라 실패한다. 검증하려는 것은 "인용이 정상 초록까지
    걸러내지 않는가"이지 "코퍼스가 적재돼 있는가"가 아니다.
    """
    from paper_assistant.db.load import load_papers
    from paper_assistant.retrieval.hybrid_search import _fulltext_search

    load_papers([sample_paper])
    with cursor() as cur:
        hits = _fulltext_search(
            cur,
            "graph neural network molecular property prediction message passing",
            limit=10)
    assert hits, "정상 초록이 하나도 매칭되지 않는다면 인용이 과하게 좁힌 것"
