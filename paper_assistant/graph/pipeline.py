"""LangGraph DAG 조립 + 공개 진입점.

구조 (설계서 §2.2):

    input → retrieval → ┬ similarity_tagging ┐
                        ├ review_analysis    ┼→ synthesis → END
                        └ venue_trend        ┘

검색 이후 3개 분석 노드는 병렬. synthesis는 셋 다 끝나야 실행된다.
supervisor 없이 고정 DAG — 워크플로가 매번 동일하므로 LLM 라우팅이 불필요.
"""
import functools

from langgraph.graph import END, START, StateGraph

from paper_assistant.embedding.specter2 import Specter2Embedder
from paper_assistant.graph import nodes
from paper_assistant.graph.llm import get_llm
from paper_assistant.graph.state import PipelineState
from paper_assistant.schemas import Report

_compiled = None       # (graph, embedder) 캐시
_llm_enabled = None


def build(embedder=None, use_llm: bool | None = None):
    """DAG를 컴파일해 반환. embedder/llm을 각 노드에 바인딩한다."""
    embedder = embedder or Specter2Embedder()
    llm = get_llm(use_llm)

    def bind(fn):
        return functools.partial(fn, embedder=embedder, llm=llm)

    g = StateGraph(PipelineState)
    g.add_node("input", bind(nodes.input_node))
    g.add_node("retrieval", bind(nodes.retrieval_node))
    g.add_node("similarity_tagging", bind(nodes.similarity_tagging_node))
    g.add_node("review_analysis", bind(nodes.review_analysis_node))
    g.add_node("venue_trend", bind(nodes.venue_trend_node))
    g.add_node("synthesis", bind(nodes.synthesis_node))

    g.add_edge(START, "input")
    g.add_edge("input", "retrieval")
    # retrieval → 3개 병렬 분기
    g.add_edge("retrieval", "similarity_tagging")
    g.add_edge("retrieval", "review_analysis")
    g.add_edge("retrieval", "venue_trend")
    # 3개 모두 완료 후 synthesis (fan-in)
    g.add_edge("similarity_tagging", "synthesis")
    g.add_edge("review_analysis", "synthesis")
    g.add_edge("venue_trend", "synthesis")
    g.add_edge("synthesis", END)

    return g.compile(), embedder


def _get_compiled(use_llm: bool | None):
    global _compiled, _llm_enabled
    if _compiled is None or _llm_enabled != use_llm:
        _compiled = build(use_llm=use_llm)
        _llm_enabled = use_llm
    return _compiled


def analyze(title: str, abstract: str = "", pdf_bytes: bytes | None = None,
            use_llm: bool | None = None) -> Report:
    """공개 진입점 — 백엔드 통합 계약.

    title/abstract(또는 pdf_bytes)를 받아 종합 Report를 반환한다.
    use_llm=None이면 환경변수 PAPER_ASSISTANT_USE_LLM로 결정 (기본 off = $0).
    """
    graph, _ = _get_compiled(use_llm)
    final = graph.invoke({
        "query_title": title,
        "query_abstract": abstract,
        "pdf_bytes": pdf_bytes,
    })
    return final["report"]
