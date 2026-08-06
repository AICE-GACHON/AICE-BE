"""LangGraph DAG 조립 + 공개 진입점.

구조 (설계서 §2.2):

    input → retrieval → ┬ similarity_tagging ┐
                        ├ review_analysis    ┼→ synthesis → END
                        └ venue_trend        ┘

검색 이후 3개 분석 노드는 병렬. synthesis는 셋 다 끝나야 실행된다.
supervisor 없이 고정 DAG — 워크플로가 매번 동일하므로 LLM 라우팅이 불필요.
"""
import functools
import threading

from langgraph.graph import END, START, StateGraph

from paper_assistant import config
from paper_assistant.embedding.specter2 import Specter2Embedder
from paper_assistant.graph import nodes
from paper_assistant.graph.llm import get_llm
from paper_assistant.graph.state import PipelineState
from paper_assistant.schemas import Report

# 백엔드는 analyze()를 BackgroundTasks(=스레드풀)에서 부른다. 락이 없으면 첫 요청
# 두 건이 겹칠 때 두 스레드가 동시에 SPECTER2를 로드한다 (각 수십 초 + 수백 MB).
_build_lock = threading.Lock()

# embedder는 LLM 설정과 무관하므로 따로 캐시한다. 같이 묶어두면 use_llm이 바뀔
# 때마다 모델을 처음부터 다시 로드하게 된다.
_embedder: Specter2Embedder | None = None
_graphs: dict[bool, object] = {}     # use_llm -> 컴파일된 그래프


def build(embedder=None, use_llm: bool | None = None):
    """DAG를 컴파일해 (graph, embedder)를 반환. 각 노드에 embedder/llm을 바인딩한다."""
    embedder = embedder or Specter2Embedder()
    llm = get_llm(use_llm)

    def bind(fn):
        return functools.partial(fn, embedder=embedder, llm=llm)

    g = StateGraph(PipelineState)
    g.add_node("input", bind(nodes.input_node))
    g.add_node("retrieval", bind(nodes.retrieval_node))
    g.add_node("llm_rerank", bind(nodes.llm_rerank_node))
    g.add_node("similarity_tagging", bind(nodes.similarity_tagging_node))
    g.add_node("review_analysis", bind(nodes.review_analysis_node))
    g.add_node("venue_trend", bind(nodes.venue_trend_node))
    g.add_node("synthesis", bind(nodes.synthesis_node))

    g.add_edge(START, "input")
    g.add_edge("input", "retrieval")
    # retrieval → 4개 병렬 분기. llm_rerank는 나머지 셋과 독립이라 같은 층에 둔다
    # (셋 다 similar_papers만 읽고 서로의 결과를 쓰지 않는다).
    g.add_edge("retrieval", "llm_rerank")
    g.add_edge("retrieval", "similarity_tagging")
    g.add_edge("retrieval", "review_analysis")
    g.add_edge("retrieval", "venue_trend")
    # 넷 모두 완료 후 synthesis (fan-in)
    g.add_edge("llm_rerank", "synthesis")
    g.add_edge("similarity_tagging", "synthesis")
    g.add_edge("review_analysis", "synthesis")
    g.add_edge("venue_trend", "synthesis")
    g.add_edge("synthesis", END)

    return g.compile(), embedder


def _get_graph(use_llm: bool):
    """컴파일된 그래프를 캐시에서 꺼낸다 (없으면 만든다). 스레드 안전."""
    graph = _graphs.get(use_llm)
    if graph is not None:
        return graph
    with _build_lock:
        # 락을 기다리는 사이 다른 스레드가 이미 만들었을 수 있다.
        graph = _graphs.get(use_llm)
        if graph is None:
            global _embedder
            graph, _embedder = build(embedder=_embedder, use_llm=use_llm)
            _graphs[use_llm] = graph
    return graph


def warmup(use_llm: bool | None = None) -> None:
    """SPECTER2와 그래프를 미리 로드한다 (서버 기동 시 첫 요청 지연 제거용)."""
    _get_graph(config.USE_LLM if use_llm is None else use_llm)


def analyze(title: str, abstract: str = "", pdf_bytes: bytes | None = None,
            use_llm: bool | None = None) -> Report:
    """공개 진입점 — 백엔드 통합 계약.

    title/abstract(또는 pdf_bytes)를 받아 종합 Report를 반환한다.
    use_llm=None이면 설정값 PAPER_ASSISTANT_USE_LLM으로 결정 (기본 off = $0).
    """
    resolved = config.USE_LLM if use_llm is None else use_llm
    final = _get_graph(resolved).invoke({
        "query_title": title,
        "query_abstract": abstract,
        "pdf_bytes": pdf_bytes,
    })
    report: Report = final["report"]
    # 결과가 실제로 LLM으로 만들어졌는지를 Report 자체에 남긴다 — 호출자가 설정값만
    # 보고 기록하면 "켰다고 생각했는데 스텁이 나온" 경우를 구분할 수 없다.
    report.used_llm = resolved
    return report
