"""진행 이벤트 — 배선(스트림으로 실제로 나오는가)과 문구(무엇을 말하는가).

문구까지 테스트하는 이유는 이 값이 **사용자 화면에 그대로 나가기 때문**이다.
특히 스텁 선정과 LLM 선정을 같은 말로 덮지 않는 것은 규약이다 (Report.used_llm).
"""
import pytest

from paper_assistant.graph import nodes, progress
from paper_assistant.retrieval.hybrid_search import SearchResult
from paper_assistant.schemas import ProgressEvent, RetrievalConfidence


def _fake_paper(pid, cosine=0.95):
    return SearchResult(
        paper_id=pid, openreview_id=f"or{pid}", title=f"P{pid}", abstract="abs",
        venue="ICLR 2024", year=2024, decision="reject", rrf_score=0.03,
        vector_rank=pid, fts_rank=pid, cosine=cosine, match_type="both",
        meta_review=None)


@pytest.fixture
def captured(monkeypatch):
    """nodes.emit을 가로채 이벤트를 모은다.

    노드를 그래프 없이 직접 부르는 테스트라 진짜 emit은 no-op이 된다 — 그래서
    문구를 보려면 가로채야 한다. 배선 자체는 아래 스트림 테스트가 따로 본다.
    """
    events: list[ProgressEvent] = []

    def fake_emit(step, label, *, done=False, detail=None):
        events.append(progress.event(step, label, done=done, detail=detail))

    monkeypatch.setattr(nodes, "emit", fake_emit)
    return events


# ------------------------------------------------------------------ 배선

def test_emit_outside_a_graph_is_silent():
    """**이게 깨지면 노드 단위 테스트가 전부 그래프를 띄워야 한다.**

    langgraph의 get_stream_writer()는 runnable 컨텍스트 밖에서 RuntimeError를 낸다.
    진행 표시 하나 때문에 노드가 단독으로 못 돌게 되면 안 된다.
    """
    progress.emit("retrieval", "아무도 안 듣는 중")     # 예외가 나지 않으면 통과


def test_node_still_runs_when_called_directly():
    """진행 이벤트를 넣은 뒤에도 노드 직접 호출이 살아 있어야 한다."""
    out = nodes.synthesis_node(
        {"query_title": "Q", "query_abstract": "A", "similar_papers": []},
        embedder=None, llm=None)
    assert out["report"].query_title == "Q"


def test_progress_events_travel_through_the_custom_stream():
    """emit → custom 스트림 → 호출자. 장난감 그래프로 전송 경로만 확인한다.

    ProgressEvent(Pydantic 객체)가 dict로 뭉개지지 않고 그대로 건너오는지도 함께
    본다 — 뭉개지면 호출자가 .label로 못 읽는다.
    """
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    class S(TypedDict, total=False):
        n: int

    def node(state: S) -> dict:
        progress.emit("retrieval", "찾는 중")
        progress.emit("retrieval", "찾았어요", done=True, detail="후보 3편")
        return {"n": 1}

    g = StateGraph(S)
    g.add_node("only", node)
    g.add_edge(START, "only")
    g.add_edge("only", END)

    events, final = [], None
    for mode, chunk in g.compile().stream({"n": 0},
                                          stream_mode=["custom", "values"]):
        (events.append(chunk) if mode == "custom" else None)
        if mode == "values":
            final = chunk

    assert [type(e) for e in events] == [ProgressEvent, ProgressEvent]
    assert [(e.step, e.done) for e in events] == [("retrieval", False),
                                                  ("retrieval", True)]
    assert events[1].detail == "후보 3편"
    assert events[0].at <= events[1].at
    # values의 마지막이 최종 상태라는 전제 위에 analyze()가 서 있다.
    assert final == {"n": 1}


def test_every_step_id_is_declared():
    """노드가 쓰는 step id가 STEPS에 다 있어야 한다 — 화면이 이 목록으로 순서를
    정하므로, 목록에 없는 id가 오면 그 단계는 화면에서 사라진다."""
    import re

    from paper_assistant.graph import pipeline

    # pipeline도 본다 — prepare 단계는 노드가 아니라 거기서 나온다.
    used = set()
    for module in (nodes, pipeline):
        with open(module.__file__, encoding="utf-8") as f:
            used |= set(re.findall(r'\b(?:emit|event)\(\s*"([a-z_]+)"', f.read()))
    assert "prepare" in used                       # 스캔이 pipeline을 실제로 읽었다
    assert used <= set(progress.STEPS), f"미선언 step: {used - set(progress.STEPS)}"


# ------------------------------------------------------------------ 문구

def test_stub_selection_never_claims_the_llm_judged_it(captured):
    """LLM이 꺼져 있을 때의 문구가 '골랐다'로 읽히면 개편 효과를 잴 수 없다."""
    papers = [_fake_paper(i) for i in range(1, 4)]
    nodes.llm_rerank_node(
        {"similar_papers": papers, "confidence": nodes.confidence_from(papers)},
        embedder=None, llm=None)

    end = [e for e in captured if e.step == "rerank" and e.done][0]
    assert "본문을 대조" not in end.label
    assert "검색 상위" in end.label
    assert end.detail and "하지 않았어요" in end.detail


def test_weak_confidence_explains_why_selection_was_skipped(captured):
    """weak이면 재정렬을 통째로 건너뛴다 — 알리지 않으면 분석이 이유 없이 짧게
    끝난 것으로만 보인다."""
    papers = [_fake_paper(i, cosine=0.86) for i in range(1, 4)]
    confidence = nodes.confidence_from(papers)
    assert not confidence.is_reliable          # 전제 확인

    out = nodes.llm_rerank_node(
        {"similar_papers": papers, "confidence": confidence},
        embedder=None, llm=object())           # llm이 있어도 건너뛴다
    assert out["selections"] == []

    end = [e for e in captured if e.step == "rerank" and e.done][0]
    assert "건너뛰" in end.label
    assert end.detail == confidence.message


def test_no_candidates_is_reported_not_swallowed(captured):
    nodes.llm_rerank_node({"similar_papers": []}, embedder=None, llm=None)
    assert [(e.step, e.done) for e in captured] == [("rerank", False), ("rerank", True)]
    assert "후보가 없어" in captured[-1].label


def test_progress_never_leaks_the_diagnostic_cosine(captured):
    """confidence.evidence는 '사용자에게 노출 금지'다 (schemas.RetrievalConfidence)."""
    confidence = RetrievalConfidence(level="weak", message="믿기 어려워요",
                                     is_reliable=False, evidence=0.8612)
    nodes.llm_rerank_node({"similar_papers": [_fake_paper(1)], "confidence": confidence},
                          embedder=None, llm=None)
    for ev in captured:
        assert "0.86" not in (ev.label + (ev.detail or ""))


def test_synthesis_brackets_its_work(captured):
    nodes.synthesis_node({"query_title": "Q", "query_abstract": "A",
                          "similar_papers": []}, embedder=None, llm=None)
    assert [(e.step, e.done) for e in captured] == [("synthesis", False),
                                                    ("synthesis", True)]


def test_review_fetch_is_skipped_when_nothing_was_selected(captured):
    """고른 논문이 없으면 단계 자체를 세우지 않는다 — 이유는 rerank가 이미 말했다."""
    out = nodes.review_fetch_node({"selections": []}, embedder=None, llm=None)
    assert out["selected_papers"] == []
    assert captured == []


# ------------------------------------------------- analyze() (invoke → stream)
#
# 진짜 노드는 DB와 SPECTER2를 타므로, 그래프를 장난감으로 갈아끼우고 analyze()가
# 스트림을 제대로 소비하는지만 본다. 바꾼 것이 정확히 그 부분이다.

@pytest.fixture
def toy_graph(monkeypatch):
    """_get_graph를 가로채 '이벤트 2건 + report 1개'를 내는 그래프로 바꾼다."""
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    from paper_assistant.graph import pipeline
    from paper_assistant.schemas import Report

    class S(TypedDict, total=False):
        query_title: str
        query_abstract: str
        pdf_bytes: bytes | None
        report: Report

    def node(state: S) -> dict:
        progress.emit("retrieval", "찾는 중")
        progress.emit("retrieval", "찾았어요", done=True)
        return {"report": Report(query_title=state["query_title"],
                                 query_abstract=state.get("query_abstract", ""))}

    g = StateGraph(S)
    g.add_node("only", node)
    g.add_edge(START, "only")
    g.add_edge("only", END)
    compiled = g.compile()

    # 그래프를 얻는 순간을 타임라인에 남긴다 — 이 호출이 곧 SPECTER2 로드라,
    # 진행 이벤트가 그 앞에 오는지를 실제로 겨룰 수 있어야 한다.
    timeline: list[str] = []
    monkeypatch.setattr(pipeline, "_get_graph",
                        lambda use_llm: (timeline.append("load"), compiled)[1])
    return timeline


def test_analyze_returns_the_final_state_and_streams_events(toy_graph):
    from paper_assistant.graph.pipeline import analyze

    events: list[ProgressEvent] = []
    report = analyze("Graph nets", "abstract", use_llm=False,
                     on_event=events.append)

    assert report.query_title == "Graph nets"
    assert report.used_llm is False
    # prepare(시작/끝)는 그래프 밖에서, retrieval은 노드 안에서 나온다 — 두 경로가
    # 하나의 순서로 합쳐져야 화면이 단계를 이어 그릴 수 있다.
    assert [(e.step, e.done) for e in events] == [
        ("prepare", False), ("prepare", True),
        ("retrieval", False), ("retrieval", True)]


def test_analyze_without_on_event_behaves_the_same(toy_graph):
    """진행을 안 쓰는 호출자(기존 코드)가 그대로 돌아야 한다."""
    from paper_assistant.graph.pipeline import analyze

    assert analyze("Q", "A", use_llm=False).query_title == "Q"


def test_a_broken_on_event_does_not_kill_the_analysis(toy_graph):
    """진행 표시가 실패해서 분석이 죽는 것은 앞뒤가 바뀐 일이다."""
    from paper_assistant.graph.pipeline import analyze

    def boom(ev):
        raise RuntimeError("DB가 죽었다")

    assert analyze("Q", "A", use_llm=False, on_event=boom).query_title == "Q"


def test_prepare_event_precedes_the_model_load(toy_graph):
    """첫 분석의 가장 긴 침묵은 SPECTER2 로드다. 그 **전에** 알려야 의미가 있다.

    로드가 끝난 뒤에 알리면 사용자는 그 수십 초를 여전히 빈 화면으로 본다 —
    이 순서가 뒤집히면 prepare 단계를 넣은 이유가 통째로 없어진다.
    """
    from paper_assistant.graph.pipeline import analyze

    timeline = toy_graph        # _get_graph가 "load"를 여기 남긴다
    analyze("Q", "A", use_llm=False,
            on_event=lambda ev: timeline.append(f"{ev.step}:{ev.done}"))

    assert timeline[:3] == ["prepare:False", "load", "prepare:True"]


def test_load_warning_appears_only_while_the_model_is_cold(toy_graph, monkeypatch):
    """'시간이 걸려요'를 매번 말하면 정작 오래 걸리는 첫 분석에서 안 읽힌다."""
    from paper_assistant.graph import pipeline

    events: list[ProgressEvent] = []
    monkeypatch.setattr(pipeline, "_graphs", {})            # 차가운 상태
    pipeline.analyze("Q", "A", use_llm=False, on_event=events.append)
    assert "시간이 걸려요" in (events[0].detail or "")

    events.clear()
    monkeypatch.setattr(pipeline, "_graphs", {False: object()})   # 이미 로드됨
    pipeline.analyze("Q", "A", use_llm=False, on_event=events.append)
    assert events[0].detail is None
