"""근거 풀 조립과 인용 검증 (DB·LLM 불필요).

여기서 지키는 보장은 하나다: **화면에 남은 인용은 전부 실제 원문으로 역추적된다.**
검증이 없으면 "인용해 달라"는 부탁일 뿐이고, 모델이 [E9]를 지어내도 화면에는 근거가
달린 것처럼 보인다.

⚠️ 보장의 범위는 라벨의 실재까지다. 실재하는 라벨을 엉뚱한 문장에 붙인 경우는
잡히지 않는다 (evidence.py 모듈 주석 참고).
"""
from paper_assistant.graph.evidence import (
    MAX_META_REVIEWS, MAX_POINTS_PER_PAPER, MAX_TEXT, build_evidence_for_selected,
    format_for_prompt, validate_citations)
from paper_assistant.schemas import SelectedPaper


def _paper(pid, title="논문", decision="reject", meta_review=None):
    return SelectedPaper(
        paper_id=pid, openreview_id=f"or{pid}", title=title, venue="ICLR 2025",
        year=2025, decision=decision,
        openreview_url=f"https://openreview.net/forum?id=or{pid}",
        pdf_url=f"https://openreview.net/pdf?id=or{pid}",
        rank=pid, reason="비슷함", confidence="high", meta_review=meta_review)


def _point(text, point_id=1, aspect="baselines", from_unsplit=False):
    return {"point_id": point_id, "aspect": aspect, "text": text,
            "from_unsplit": from_unsplit}


# ------------------------------------------------------------- 근거 풀 조립

def test_evidence_comes_from_the_selected_papers():
    """근거는 **화면에 보이는 논문**에서 나와야 한다.

    집계에서 뽑던 시절에는 화면에 없는 논문의 문장이 근거로 붙을 수 있었다.
    """
    papers = [_paper(1, "논문1"), _paper(2, "논문2")]
    ev = build_evidence_for_selected(
        papers, {1: [_point("QM9로만 평가했다", 101)],
                 2: [_point("베이스라인이 약하다", 202)]})
    assert [e.label for e in ev] == ["E1", "E2"]
    assert {e.paper_id for e in ev} == {1, 2}
    assert ev[0].review_point_id == 101
    assert ev[0].paper_title == "논문1"


def test_points_are_capped_per_paper():
    """한 논문이 근거 풀을 독점하면 나머지 논문의 지적이 프롬프트에서 사라진다."""
    ev = build_evidence_for_selected(
        [_paper(1)], {1: [_point(f"지적{i}", i) for i in range(10)]})
    assert len(ev) == MAX_POINTS_PER_PAPER


def test_meta_reviews_get_their_own_label_space():
    """AC 총평은 개별 리뷰보다 신호가 강해 따로 인용할 수 있어야 한다."""
    papers = [_paper(1, meta_review="AC: 실험이 부족하다는 데 동의"),
              _paper(2, "논문2", "accept-poster", meta_review="AC: 통과")]
    ev = build_evidence_for_selected(papers, {})
    metas = [e for e in ev if e.kind == "meta_review"]
    assert [m.label for m in metas] == ["M1", "M2"]
    assert metas[1].decision == "accept-poster"


def test_meta_reviews_are_capped():
    papers = [_paper(i, meta_review=f"AC {i}") for i in range(1, 9)]
    ev = build_evidence_for_selected(papers, {})
    assert len([e for e in ev if e.kind == "meta_review"]) == MAX_META_REVIEWS


def test_missing_meta_review_does_not_consume_a_label():
    """총평이 없는 논문 때문에 M2가 건너뛰어지면 라벨이 어긋난다."""
    papers = [_paper(1), _paper(2, meta_review="AC 총평")]
    metas = [e for e in build_evidence_for_selected(papers, {})
             if e.kind == "meta_review"]
    assert [m.label for m in metas] == ["M1"]
    assert metas[0].paper_id == 2


def test_long_text_is_truncated():
    """프롬프트가 길어지면 모델이 요약 대신 문장만 베낀다."""
    ev = build_evidence_for_selected([_paper(1)], {1: [_point("가" * 5000)]})
    assert len(ev[0].text) == MAX_TEXT


def test_unsplit_provenance_survives_into_the_pool():
    """미분리 리뷰에서 나온 문장은 '지적'이라 단정할 수 없다 — 출처가 남아야 한다."""
    ev = build_evidence_for_selected(
        [_paper(1)], {1: [_point("본문 전체", from_unsplit=True)]})
    assert ev[0].from_unsplit_review is True


def test_no_points_and_no_meta_gives_an_empty_pool():
    assert build_evidence_for_selected([_paper(1)], {}) == []


def test_prompt_format_leads_with_the_label():
    """모델이 참조할 키가 맨 앞에 있어야 한다."""
    ev = build_evidence_for_selected(
        [_paper(1, meta_review="AC")], {1: [_point("지적")]})
    out = format_for_prompt(ev)
    assert list(out[0])[0] == "label"
    assert out[0]["aspect"] == "baselines"
    assert out[1]["decision"] == "reject"


# -------------------------------------------------------------- 인용 검증

def test_invented_labels_are_stripped():
    """**핵심 보증.** 지어낸 라벨이 통과하면 검증은 부탁일 뿐이다."""
    ev = build_evidence_for_selected([_paper(1)], {1: [_point("지적")]})
    text, used = validate_citations("근거가 있다[E1]. 없는 것도 있다[E9].", ev)
    assert "[E9]" not in text
    assert used == ["E1"]


def test_a_block_of_only_invented_labels_disappears_entirely():
    """대괄호만 남으면 화면에 빈 각주가 뜬다."""
    ev = build_evidence_for_selected([_paper(1)], {1: [_point("지적")]})
    text, used = validate_citations("근거 없는 문장이다[E7, E8].", ev)
    assert "[" not in text
    assert used == []


def test_mixed_block_keeps_only_the_real_labels():
    ev = build_evidence_for_selected(
        [_paper(1)], {1: [_point("가", 1), _point("나", 2)]})
    text, used = validate_citations("문장이다[E1, E9, E2].", ev)
    assert "[E1, E2]" in text
    assert used == ["E1", "E2"]


def test_duplicate_citations_are_recorded_once():
    ev = build_evidence_for_selected([_paper(1)], {1: [_point("지적")]})
    _, used = validate_citations("하나[E1]. 둘[E1].", ev)
    assert used == ["E1"]


def test_markdown_links_are_not_mangled():
    """[텍스트](url)을 인용 표기로 오인하면 링크가 깨진다."""
    ev = build_evidence_for_selected([_paper(1)], {1: [_point("지적")]})
    text, _ = validate_citations("[OpenReview](https://openreview.net) 참고[E1].", ev)
    assert "[OpenReview](https://openreview.net)" in text


def test_empty_pool_strips_every_citation():
    """근거가 없는데 인용이 남으면 검증된 것처럼 보인다."""
    text, used = validate_citations("근거가 있는 척한다[E1].", [])
    assert "[E1]" not in text
    assert used == []


def test_text_without_citations_is_untouched():
    ev = build_evidence_for_selected([_paper(1)], {1: [_point("지적")]})
    text, used = validate_citations("인용이 없는 문장.", ev)
    assert text == "인용이 없는 문장."
    assert used == []
