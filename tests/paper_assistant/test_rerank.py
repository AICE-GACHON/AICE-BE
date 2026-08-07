"""2단계 LLM 재정렬 — 선정 검증과 호출 회피 조건 (DB·API 불필요).

여기서 검증하는 것은 **모델이 잘 고르는가**가 아니라 (그건 코드로 검증할 수 없다)
**모델이 잘못 답했을 때 우리가 막는가**다. 검색 후보에 없는 논문이 화면에 뜨면
존재하지 않는 논문의 리뷰를 조회하게 되고, 조회가 비면 사용자에게는 그냥 빈 카드로
보인다 — 지어냈다는 사실이 드러나지 않는 종류의 실패다.
"""
import pytest

from paper_assistant.graph.nodes import (
    RERANK_LIMIT, _stub_selections, _validated_selections, llm_rerank_node)
from paper_assistant.retrieval.hybrid_search import SearchResult
from paper_assistant.schemas import RetrievalConfidence


def _paper(pid: int, year: int = 2025) -> SearchResult:
    return SearchResult(
        paper_id=pid, openreview_id=f"or{pid}", title=f"Paper {pid}",
        abstract="abstract text", venue="ICLR 2025", year=year,
        decision="reject", rrf_score=0.03, vector_rank=1, fts_rank=1,
        cosine=0.95, match_type="both")


def _sel(pid, reason="둘 다 분자 그래프에 메시지 패싱을 쓴다", confidence="high"):
    return {"paper_id": pid, "reason": reason, "confidence": confidence}


class FakeLLM:
    """structured_with_pdf만 흉내낸다. 호출 인자를 기록해 검증에 쓴다."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def structured_with_pdf(self, model, system, pdf_bytes, user, schema,
                            max_tokens, **params):
        self.calls.append({"model": model, "system": system, "pdf": pdf_bytes,
                           "user": user, "schema": schema, "params": params})
        return self.payload


# ------------------------------------------------------------ 환각 방어

def test_paper_ids_outside_the_candidate_list_are_dropped():
    """**가장 중요한 테스트.** 지어낸 id가 통과하면 검증이 부탁일 뿐이 된다."""
    papers = [_paper(1), _paper(2)]
    out = _validated_selections([_sel(1), _sel(999), _sel(2)], papers)
    assert [s.paper_id for s in out] == [1, 2]


def test_dropping_an_invalid_id_does_not_leave_a_rank_gap():
    """버린 자리 때문에 rank가 1,3으로 뛰면 화면 순서가 깨진다."""
    out = _validated_selections([_sel(999), _sel(1), _sel(2)],
                                [_paper(1), _paper(2)])
    assert [s.rank for s in out] == [1, 2]


def test_duplicate_selections_are_dropped():
    """같은 논문을 두 번 고르면 5칸 중 하나가 낭비되고 화면에 두 번 뜬다."""
    out = _validated_selections([_sel(1), _sel(1), _sel(2)],
                                [_paper(1), _paper(2)])
    assert [s.paper_id for s in out] == [1, 2]


def test_selection_count_is_capped():
    """스키마로 배열 길이를 걸 수 없으므로(구조화 출력 제약) 코드가 잘라야 한다."""
    papers = [_paper(i) for i in range(1, 11)]
    out = _validated_selections([_sel(i) for i in range(1, 11)], papers)
    assert len(out) == RERANK_LIMIT


def test_unknown_confidence_falls_back_to_low():
    """열거형 밖의 값이 그대로 화면에 나가면 안 된다."""
    out = _validated_selections([_sel(1, confidence="매우 높음")], [_paper(1)])
    assert out[0].confidence == "low"


def test_missing_paper_id_is_dropped_without_raising():
    """필드가 통째로 빠진 응답에도 검색 결과는 살아야 한다."""
    out = _validated_selections([{"reason": "r", "confidence": "high"}, _sel(1)],
                                [_paper(1)])
    assert [s.paper_id for s in out] == [1]


def test_fewer_than_the_limit_is_allowed():
    """5편을 억지로 채우면 '이 논문들이 받은 리뷰'라는 약속이 거짓이 된다."""
    out = _validated_selections([_sel(1)], [_paper(1), _paper(2), _paper(3)])
    assert len(out) == 1


def test_empty_selection_is_allowed():
    """정말 비슷한 논문이 없으면 빈 목록이 정직한 답이다."""
    assert _validated_selections([], [_paper(1)]) == []


# --------------------------------------------------- LLM을 부르지 않는 조건

def _state(**over):
    base = {"similar_papers": [_paper(1), _paper(2)], "pdf_bytes": b"%PDF-1.4",
            "confidence": RetrievalConfidence(level="strong", is_reliable=True)}
    return {**base, **over}


def test_weak_confidence_skips_the_llm_entirely():
    """도메인 밖 PDF에도 LLM은 '가장 비슷한 5편'을 성실히 골라낸다.

    여기서 막지 않으면 요리 레시피를 넣어도 그럴듯한 답이 나오고, 그 호출에 돈까지
    쓴다. 검색 신뢰도가 유일한 방어선이다.
    """
    llm = FakeLLM({"selections": [_sel(1)]})
    out = llm_rerank_node(
        _state(confidence=RetrievalConfidence(level="weak", is_reliable=False)),
        embedder=None, llm=llm)
    assert out["selections"] == []
    assert llm.calls == [], "weak인데 LLM을 불렀다"


def test_no_pdf_falls_back_to_the_stub_without_calling():
    """옛 초안(텍스트로 만든 것)은 넘길 원문이 없다."""
    llm = FakeLLM({"selections": [_sel(1)]})
    out = llm_rerank_node(_state(pdf_bytes=None), embedder=None, llm=llm)
    assert llm.calls == []
    assert [s.paper_id for s in out["selections"]] == [1, 2]


def test_no_candidates_skips_the_call():
    llm = FakeLLM({"selections": []})
    assert llm_rerank_node(_state(similar_papers=[]), embedder=None,
                           llm=llm)["selections"] == []
    assert llm.calls == []


def test_stub_marks_itself_as_not_llm_judged():
    """스텁 결과가 LLM 판정으로 오인되면 개편 효과를 측정할 수 없게 된다."""
    out = _stub_selections([_paper(1), _paper(2)])
    assert "LLM" in out[0].reason
    assert all(s.confidence == "low" for s in out)


def test_stub_respects_the_limit():
    assert len(_stub_selections([_paper(i) for i in range(1, 11)])) == RERANK_LIMIT


# ------------------------------------------------------------ 호출 형태

def test_the_pdf_is_passed_through_untouched():
    """텍스트 추출이 아니라 PDF 원본을 넘기는 것이 이 단계의 전제다."""
    llm = FakeLLM({"selections": [_sel(1)]})
    llm_rerank_node(_state(pdf_bytes=b"%PDF-1.4 real bytes"), embedder=None, llm=llm)
    assert llm.calls[0]["pdf"] == b"%PDF-1.4 real bytes"


def test_all_candidates_are_offered_to_the_model():
    """후보를 코드가 미리 잘라내면 재정렬의 의미가 없다."""
    papers = [_paper(i) for i in range(1, 51)]
    llm = FakeLLM({"selections": [_sel(1)]})
    llm_rerank_node(_state(similar_papers=papers), embedder=None, llm=llm)
    assert llm.calls[0]["user"].count('"paper_id"') == 50


def test_effort_is_higher_than_the_synthesis_step():
    """재정렬에서 LLM은 판정자 본인이다 — 여기서 아끼면 개편의 근거가 없어진다."""
    from paper_assistant.llm import RERANK_EFFORT, SONNET_EFFORT

    order = ["low", "medium", "high", "xhigh", "max"]
    assert order.index(RERANK_EFFORT) > order.index(SONNET_EFFORT)


@pytest.mark.parametrize("field", ["paper_id", "reason", "confidence"])
def test_schema_requires_every_field(field):
    """선택 필드로 두면 모델이 이유 없이 논문만 던져도 통과한다."""
    from paper_assistant.graph.nodes import _SELECTION_SCHEMA

    item = _SELECTION_SCHEMA["properties"]["selections"]["items"]
    assert field in item["required"]
    assert item["additionalProperties"] is False


def test_every_llm_call_path_logs_usage():
    """비용은 추정이 아니라 실측으로 남아야 한다.

    한 경로라도 로깅이 빠지면 그 단계의 비용만 장부에서 사라진다 — 실제로
    structured_with_pdf에만 붙어 있어 종합(text) 비용이 안 보이던 적이 있다.
    """
    import inspect as _i

    from paper_assistant.llm import ClaudeLLM

    for name in ("text", "structured_with_pdf"):
        src = _i.getsource(getattr(ClaudeLLM, name))
        assert "_log_usage" in src, f"{name}()에 사용량 로깅이 없다"


def test_schema_has_no_numeric_similarity_field():
    """유사도 점수는 만들 수 없다(설계서 §20). 스키마 자체에서 배제한다."""
    from paper_assistant.graph.nodes import _SELECTION_SCHEMA

    props = _SELECTION_SCHEMA["properties"]["selections"]["items"]["properties"]
    assert props["confidence"]["type"] == "string"
    assert not any(p.get("type") == "number" for p in props.values())
