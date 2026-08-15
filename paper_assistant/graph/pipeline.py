"""LangGraph DAG 조립 + 공개 진입점.

구조 (docs/추천_파이프라인_재설계.md):

    input → retrieval → llm_rerank → review_fetch → synthesis → END

**완전 직렬이다.** 각 단계가 앞 단계의 결과를 입력으로 쓰기 때문이다 — 검색해야
고를 수 있고, 골라야 그 논문의 리뷰를 읽을 수 있다. 병렬 분기가 있었던 것은
통계 레이어(집계·학회경향·점수분포)가 검색 결과만 보고 서로 독립이었기 때문인데,
그 층을 걷어내면서 함께 사라졌다.

supervisor 없이 고정 DAG — 워크플로가 매번 동일하므로 LLM 라우팅이 불필요.
"""
import functools
import logging
import threading
from collections.abc import Callable

from langgraph.graph import END, START, StateGraph

from paper_assistant import config
from paper_assistant.embedding.specter2 import Specter2Embedder
from paper_assistant.graph import nodes, progress
from paper_assistant.llm import get_llm
from paper_assistant.graph.state import PipelineState
from paper_assistant.schemas import ProgressEvent, Report

log = logging.getLogger(__name__)

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
    g.add_node("review_fetch", bind(nodes.review_fetch_node))
    g.add_node("synthesis", bind(nodes.synthesis_node))

    g.add_edge(START, "input")
    g.add_edge("input", "retrieval")
    g.add_edge("retrieval", "llm_rerank")
    g.add_edge("llm_rerank", "review_fetch")   # 선정된 뒤에야 리뷰를 읽을 수 있다
    g.add_edge("review_fetch", "synthesis")
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
            use_llm: bool | None = None,
            on_event: Callable[[ProgressEvent], None] | None = None) -> Report:
    """공개 진입점 — 백엔드 통합 계약.

    title/abstract(또는 pdf_bytes)를 받아 종합 Report를 반환한다.
    use_llm=None이면 설정값 PAPER_ASSISTANT_USE_LLM으로 결정 (기본 off = $0).

    on_event를 주면 단계가 바뀔 때마다 ProgressEvent로 알려준다 (없으면 조용히
    돈다). 분석은 수십 초~수 분이 걸리는데 그 사이 호출자가 아는 것이 "돌고 있다"
    뿐이라, 화면도 그 이상을 말할 수 없었다. 이벤트를 밖으로 내보내는 것은 여기
    하나뿐이고, 어디에 저장할지·어떻게 전달할지는 호출자가 정한다.

    ⚠️ **on_event에서 예외가 나도 분석은 계속된다.** 진행 표시가 실패해서 분석이
    죽는 것은 앞뒤가 바뀐 일이다 — 원인은 로그에만 남긴다.
    """
    resolved = config.USE_LLM if use_llm is None else use_llm

    def notify(ev: ProgressEvent) -> None:
        if on_event is None:
            return
        try:
            on_event(ev)
        except Exception:
            log.exception("on_event 처리 실패 (step=%s) — 분석은 계속합니다", ev.step)

    # 그래프를 얻는 이 한 줄이 **첫 분석에서 가장 긴 침묵**이다 (SPECTER2 로드
    # 수십 초). 노드 안이 아니라서 스트림으로는 잡히지 않으므로 여기서 직접 알린다.
    #
    # 안내 문구는 그 로드가 실제로 일어날 때만 붙인다 — 두 번째 분석부터는 캐시라
    # 이 구간이 순식간에 지나간다. 매번 "시간이 걸려요"라고 말하면 그 말이 곧
    # 무시되고, 정말 오래 걸리는 첫 번째에도 읽히지 않는다.
    notify(progress.event(
        "prepare", "분석을 준비하고 있어요",
        detail=("처음 한 번은 모델을 불러오느라 시간이 걸려요"
                if _graphs.get(resolved) is None else None)))
    graph = _get_graph(resolved)
    notify(progress.event("prepare", "준비를 마쳤어요", done=True))

    # invoke() 대신 stream()을 쓴다. custom은 노드가 emit한 진행 이벤트고, values는
    # 각 단계 뒤의 전체 상태라 **마지막 것이 invoke()의 반환과 같다**(실측 확인).
    # 진행을 안 쓸 때도 같은 경로로 돌린다 — 경로가 둘이면 실제로 도는 쪽만 검증된다.
    final: dict | None = None
    for mode, chunk in graph.stream(
            {"query_title": title, "query_abstract": abstract,
             "pdf_bytes": pdf_bytes},
            stream_mode=["custom", "values"]):
        if mode == "custom":
            notify(chunk)
        else:
            final = chunk

    report: Report = final["report"]
    # 결과가 실제로 LLM으로 만들어졌는지를 Report 자체에 남긴다 — 호출자가 설정값만
    # 보고 기록하면 "켰다고 생각했는데 스텁이 나온" 경우를 구분할 수 없다.
    report.used_llm = resolved
    return report
