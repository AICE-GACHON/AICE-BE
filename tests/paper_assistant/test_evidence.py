"""근거 풀 조립 + 인용 표기 검증 (RAG 근거 추적). DB/LLM 불필요."""
from dataclasses import dataclass

from paper_assistant.graph.evidence import (
    build_evidence, format_for_prompt, validate_citations)
from paper_assistant.schemas import EvidenceItem, ReviewExample, ReviewPattern


@dataclass
class FakePaper:
    paper_id: int
    title: str
    decision: str = "reject"
    meta_review: str | None = None


def _pattern(aspect, texts, paper_ids=None, point_ids=None):
    paper_ids = paper_ids or list(range(1, len(texts) + 1))
    point_ids = point_ids or [100 + i for i in range(len(texts))]
    return ReviewPattern(
        label=aspect, aspect=aspect, paper_count=len(texts), total_papers=20,
        examples=[ReviewExample(text=t, paper_id=pid, review_point_id=qid)
                  for t, pid, qid in zip(texts, paper_ids, point_ids)])


def _evidence(*labels):
    return [EvidenceItem(label=l, kind="review_point", text="t", paper_id=1)
            for l in labels]


# ------------------------------------------------------------ 근거 풀 조립

def test_review_points_get_sequential_labels():
    patterns = [_pattern("baselines", ["약점 A", "약점 B"]),
                _pattern("novelty", ["약점 C"])]
    ev = build_evidence(patterns, [FakePaper(1, "논문1"), FakePaper(2, "논문2")])
    assert [e.label for e in ev] == ["E1", "E2", "E3"]
    assert [e.kind for e in ev] == ["review_point"] * 3


def test_evidence_keeps_source_ids_for_traceability():
    """인용을 review_points.id까지 역추적할 수 있어야 근거 추적이 성립한다."""
    patterns = [_pattern("baselines", ["QM9만으로 평가"], [7], [4242])]
    ev = build_evidence(patterns, [FakePaper(7, "GNN 논문")])
    assert ev[0].review_point_id == 4242
    assert ev[0].paper_id == 7
    assert ev[0].paper_title == "GNN 논문"
    assert ev[0].aspect == "baselines"


def test_meta_reviews_are_labelled_separately():
    papers = [FakePaper(1, "논문1", "accept-poster", "AC 총평 하나"),
              FakePaper(2, "논문2", "reject", "AC 총평 둘")]
    ev = build_evidence([_pattern("baselines", ["약점"], [1])], papers)
    labels = [e.label for e in ev]
    assert labels == ["E1", "M1", "M2"]
    meta = [e for e in ev if e.kind == "meta_review"]
    assert meta[0].decision == "accept-poster"
    assert meta[0].text == "AC 총평 하나"


def test_papers_without_meta_review_are_skipped():
    papers = [FakePaper(1, "논문1", meta_review=None),
              FakePaper(2, "논문2", meta_review="   "),   # 공백만 있는 경우
              FakePaper(3, "논문3", meta_review="진짜 총평")]
    ev = build_evidence([], papers)
    assert [e.label for e in ev] == ["M1"]
    assert ev[0].paper_id == 3


def test_pool_is_capped_so_the_prompt_stays_statistical():
    """근거가 너무 많으면 모델이 통계를 무시하고 문장만 베낀다."""
    patterns = [_pattern(f"aspect{i}", [f"약점{i}a", f"약점{i}b", f"약점{i}c"])
                for i in range(8)]
    papers = [FakePaper(i, f"논문{i}", meta_review=f"총평{i}") for i in range(1, 9)]
    ev = build_evidence(patterns, papers)
    points = [e for e in ev if e.kind == "review_point"]
    metas = [e for e in ev if e.kind == "meta_review"]
    assert len(points) == 5 * 2      # MAX_ASPECTS * MAX_POINTS_PER_ASPECT
    assert len(metas) == 3           # MAX_META_REVIEWS


def test_long_text_is_truncated():
    patterns = [_pattern("baselines", ["가" * 5000])]
    papers = [FakePaper(1, "논문1", meta_review="나" * 5000)]
    ev = build_evidence(patterns, papers)
    assert len(ev[0].text) == 400
    assert len(ev[1].text) == 700


def test_format_for_prompt_leads_with_label():
    ev = build_evidence([_pattern("baselines", ["약점"], [1])],
                        [FakePaper(1, "논문1", "reject", "총평")])
    out = format_for_prompt(ev)
    assert list(out[0])[0] == "label"
    assert out[0]["aspect"] == "baselines"
    assert out[1]["decision"] == "reject"   # meta_review는 decision을 싣는다


# --------------------------------------------------------- 인용 표기 검증

def test_valid_citation_survives():
    text, used = validate_citations("이 지적이 반복된다 [E1].", _evidence("E1", "E2"))
    assert text == "이 지적이 반복된다 [E1]."
    assert used == ["E1"]


def test_invented_label_is_stripped():
    """모델이 지어낸 라벨은 지운다 — 없으면 '인용해 달라'는 부탁일 뿐이다."""
    text, used = validate_citations("근거 없는 주장 [E9].", _evidence("E1"))
    assert "E9" not in text
    assert used == []


def test_partial_block_keeps_only_valid_labels():
    text, used = validate_citations("혼합 [E1, E9, M1].", _evidence("E1", "M1"))
    assert text == "혼합 [E1, M1]."
    assert used == ["E1", "M1"]


def test_multiple_citations_are_collected_in_order_without_duplicates():
    _, used = validate_citations("A [M1]. B [E1]. C [E1] 또 [E2].",
                                 _evidence("E1", "E2", "M1"))
    assert used == ["M1", "E1", "E2"]


def test_markdown_links_are_not_damaged():
    """[텍스트](url) 형태를 인용으로 오인해 부수면 안 된다."""
    src = "자세히는 [여기](https://openreview.net/forum?id=x)를 보라 [E1]."
    text, used = validate_citations(src, _evidence("E1"))
    assert "[여기](https://openreview.net/forum?id=x)" in text
    assert used == ["E1"]


def test_empty_evidence_pool_strips_everything():
    text, used = validate_citations("주장 [E1] 그리고 [M2].", [])
    assert "E1" not in text and "M2" not in text
    assert used == []


def test_text_without_citations_is_unchanged():
    src = "## 유사 논문 20편\n- 이 주제 특유의 지적 없음"
    text, used = validate_citations(src, _evidence("E1"))
    assert text == src
    assert used == []


def test_newlines_survive_cleanup():
    """공백 정리가 줄바꿈까지 먹으면 마크다운이 깨진다."""
    text, _ = validate_citations("첫 줄 [E9]\n- 둘째 줄\n- 셋째 줄", _evidence("E1"))
    assert text.count("\n") == 2
