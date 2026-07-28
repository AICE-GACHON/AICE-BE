"""파이프라인 구조 테스트 — DAG 배선과 노드 로직 (모델/LLM 없이).

무거운 SPECTER2 로드를 피하려고 embedder를 가짜로 주입하고,
DB가 필요한 노드는 별도 통합 테스트(test_db_integration 계열)에 맡긴다.
여기서는 순수 로직(노드 반환 형태, 종합 조립)만 검증한다.
"""
import pytest

from paper_assistant.graph import nodes
from paper_assistant.graph.state import PipelineState
from paper_assistant.retrieval.hybrid_search import SearchResult
from paper_assistant.schemas import (
    Report, ResubmissionFlow, ReviewExample, ReviewPattern, VenueTrend)


def _fake_paper(pid, title, decision="reject", cosine=0.95,
                match_type="both", vector_rank=None, meta_review=None):
    return SearchResult(
        paper_id=pid, openreview_id=f"or{pid}", title=title, abstract="abs",
        venue="ICLR 2024", year=2024, decision=decision, rrf_score=0.03,
        vector_rank=pid if vector_rank is None else vector_rank,
        fts_rank=pid, cosine=cosine, match_type=match_type,
        meta_review=meta_review)


def _example(text="이 논문은 QM9만으로 평가했다", paper_id=1, point_id=101):
    return ReviewExample(text=text, paper_id=paper_id, review_point_id=point_id)


def test_tagging_node_stub_returns_empty_tags_without_llm():
    state: PipelineState = {"query_title": "Q", "query_abstract": "A",
                            "similar_papers": [_fake_paper(1, "P1")]}
    out = nodes.similarity_tagging_node(state, embedder=None, llm=None)
    assert out["similarity_tags"] == {1: []}


def test_synthesis_assembles_report_without_llm():
    state: PipelineState = {
        "query_title": "Graph nets",
        "query_abstract": "abstract",
        "similar_papers": [_fake_paper(1, "P1", "accept-poster"),
                           _fake_paper(2, "P2", "reject")],
        "similarity_tags": {1: [], 2: []},
        "review_patterns": [ReviewPattern(
            label="weak baselines", aspect="baselines",
            paper_count=2, total_papers=2, examples=[_example()])],
        "venue_trends": [VenueTrend(venue="ICLR 2024", paper_count=2,
                                    accept_count=1, accept_rate=0.5)],
        "resubmission_flows": [ResubmissionFlow(
            from_venue="ICLR 2024", to_venue="NeurIPS 2024", count=3)],
    }
    out = nodes.synthesis_node(state, embedder=None, llm=None)
    report = out["report"]
    assert isinstance(report, Report)
    assert len(report.similar_papers) == 2
    assert report.similar_papers[0].rank == 1
    assert report.similar_papers[0].match_type == "both"
    assert report.confidence.level == "strong"     # cosine 0.95 → 도메인 안
    assert report.review_patterns[0].aspect == "baselines"
    assert report.resubmission_flows[0].from_venue == "ICLR 2024"
    assert report.resubmission_flows[0].count == 3
    assert "유사 논문 2편" in report.summary_markdown


def test_report_is_json_serializable():
    """백엔드 전달용 — Pydantic → JSON 왕복."""
    state: PipelineState = {
        "query_title": "Q", "query_abstract": "A",
        "similar_papers": [_fake_paper(1, "P1")],
        "similarity_tags": {1: []}, "review_patterns": [], "venue_trends": [],
    }
    report = nodes.synthesis_node(state, embedder=None, llm=None)["report"]
    dumped = report.model_dump_json()
    restored = Report.model_validate_json(dumped)
    assert restored.query_title == "Q"


def _synthesize(papers):
    state: PipelineState = {
        "query_title": "Q", "query_abstract": "A", "similar_papers": papers,
        "similarity_tags": {p.paper_id: [] for p in papers},
        "review_patterns": [], "venue_trends": [],
    }
    return nodes.synthesis_node(state, embedder=None, llm=None)["report"]


def test_weak_confidence_warning_leads_the_summary():
    """신뢰할 수 없는 결과는 요약 맨 앞에서 경고해야 한다 — 뒤에 묻으면 안 읽힌다."""
    papers = [_fake_paper(i, f"P{i}", cosine=0.86) for i in range(1, 4)]
    report = _synthesize(papers)
    assert report.confidence.level == "weak"
    assert not report.confidence.is_reliable
    assert report.summary_markdown.lstrip().startswith(">")
    assert "신뢰하지 마세요" in report.summary_markdown.splitlines()[0]


def test_strong_confidence_adds_no_warning():
    papers = [_fake_paper(i, f"P{i}", cosine=0.95) for i in range(1, 4)]
    report = _synthesize(papers)
    assert report.confidence.is_reliable
    assert not report.summary_markdown.lstrip().startswith(">")


def test_confidence_ignores_fts_only_papers():
    """FTS로만 걸린 논문은 코사인이 없다 — 0점으로 세면 신뢰도가 왜곡된다."""
    papers = [_fake_paper(1, "vec", cosine=0.96),
              _fake_paper(2, "vec", cosine=0.95),
              _fake_paper(3, "fts", cosine=None, match_type="lexical",
                          vector_rank=None)]
    report = _synthesize(papers)
    assert report.confidence.level == "strong"       # 0.955 평균, 3번은 제외
    assert report.similar_papers[2].match_type == "lexical"


def test_confidence_uses_vector_rank_order_not_result_order():
    """RRF 결과 순서 ≠ 벡터 순위. 상위 코사인을 제대로 골라야 한다."""
    papers = [_fake_paper(1, "low", cosine=0.86, vector_rank=50),
              _fake_paper(2, "high", cosine=0.97, vector_rank=1),
              _fake_paper(3, "high", cosine=0.96, vector_rank=2)]
    report = _synthesize(papers)
    # 벡터 순위 1,2,50 순으로 정렬되어 top-3 평균 (0.97+0.96+0.86)/3 = 0.93
    assert report.confidence.evidence == pytest.approx(0.93, abs=0.001)


def test_graph_compiles_with_fake_embedder():
    """DAG가 컴파일되고 노드/엣지가 연결되는지 (실행은 안 함)."""
    from paper_assistant.graph.pipeline import build

    class FakeEmbedder:
        dim = 768
    graph, _ = build(embedder=FakeEmbedder(), use_llm=False)
    node_names = set(graph.get_graph().nodes)
    assert {"input", "retrieval", "similarity_tagging",
            "review_analysis", "venue_trend", "synthesis"} <= node_names


# ------------------------------------------------- 근거 추적 (RAG grounding)

def _grounded_state(meta_review=None):
    papers = [_fake_paper(1, "P1", "accept-poster", meta_review=meta_review),
              _fake_paper(2, "P2", "reject")]
    return {
        "query_title": "Graph nets", "query_abstract": "abstract",
        "similar_papers": papers,
        "similarity_tags": {1: [], 2: []},
        "review_patterns": [ReviewPattern(
            label="베이스라인 비교 부족", aspect="baselines",
            paper_count=2, total_papers=2, lift=1.6, p_value=0.01,
            is_distinctive=True,
            examples=[_example("QM9 하나로만 평가했다", paper_id=1, point_id=501)])],
        "venue_trends": [], "resubmission_flows": [],
    }


def test_evidence_pool_is_filled_without_llm():
    """근거 목록은 생성 결과가 아니라 검색 결과다 — LLM을 꺼도 채워져야 한다."""
    report = nodes.synthesis_node(_grounded_state(), embedder=None, llm=None)["report"]
    assert [e.label for e in report.evidence] == ["E1"]
    assert report.evidence[0].text == "QM9 하나로만 평가했다"
    assert report.evidence[0].review_point_id == 501
    assert report.evidence[0].paper_title == "P1"


def test_meta_review_enters_the_evidence_pool():
    """AC 총평은 개별 리뷰보다 신호가 강해 근거로 인용할 수 있어야 한다."""
    state = _grounded_state(meta_review="AC: 실험이 부족하다는 데 리뷰어들이 동의")
    report = nodes.synthesis_node(state, embedder=None, llm=None)["report"]
    meta = [e for e in report.evidence if e.kind == "meta_review"]
    assert len(meta) == 1
    assert meta[0].label == "M1"
    assert meta[0].decision == "accept-poster"


def test_stub_summary_cites_and_records_citations():
    report = nodes.synthesis_node(_grounded_state(), embedder=None, llm=None)["report"]
    assert "[E1]" in report.summary_markdown
    assert report.citations == ["E1"]


def test_every_citation_resolves_to_real_evidence():
    """화면에 남은 인용은 전부 실제 원문을 가리켜야 한다 (근거 추적의 핵심 보증)."""
    report = nodes.synthesis_node(_grounded_state("AC 총평"), embedder=None,
                                  llm=None)["report"]
    labels = {e.label for e in report.evidence}
    assert set(report.citations) <= labels


def test_no_citations_when_there_is_nothing_to_cite():
    state = _grounded_state()
    state["review_patterns"] = []
    report = nodes.synthesis_node(state, embedder=None, llm=None)["report"]
    assert report.evidence == []
    assert report.citations == []
    assert "[E" not in report.summary_markdown
