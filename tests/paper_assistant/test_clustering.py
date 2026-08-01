"""리뷰 지적항목 집계 로직 테스트 (모델/DB 불필요)."""
import pytest

from paper_assistant.graph.clustering import (
    aggregate_by_aspect, binom_tail, fisher_exact_less)


def _pt(paper_id, aspect, text="a criticism that is long enough to keep"):
    return {"paper_id": paper_id, "aspect": aspect, "text": text}


def test_aggregate_counts_distinct_papers_per_aspect():
    points = [_pt(1, "baselines"), _pt(2, "baselines"),
              _pt(2, "baselines"),   # 같은 논문 중복 → 1편으로
              _pt(3, "clarity")]
    patterns = aggregate_by_aspect(points, total_papers=5, min_papers=1)
    by = {p.aspect: p for p in patterns}
    assert by["baselines"].paper_count == 2   # 논문 1,2
    assert by["clarity"].paper_count == 1
    assert by["baselines"].total_papers == 5


def test_aggregate_excludes_other_by_default():
    points = [_pt(1, "other"), _pt(2, "other"), _pt(3, "baselines"), _pt(4, "baselines")]
    patterns = aggregate_by_aspect(points, total_papers=4)
    assert all(p.aspect != "other" for p in patterns)


def test_aggregate_respects_min_papers():
    points = [_pt(1, "novelty"), _pt(2, "baselines"), _pt(3, "baselines")]
    patterns = aggregate_by_aspect(points, total_papers=3, min_papers=2)
    assert {p.aspect for p in patterns} == {"baselines"}  # novelty는 1편이라 제외


def test_aggregate_sorted_by_paper_count():
    points = ([_pt(i, "baselines") for i in range(5)] +
              [_pt(i, "clarity") for i in range(2)])
    patterns = aggregate_by_aspect(points, total_papers=5, min_papers=2)
    assert patterns[0].aspect == "baselines"
    assert patterns[0].paper_count > patterns[1].paper_count


def test_aggregate_examples_from_distinct_papers():
    points = [_pt(1, "clarity", "paper one is unclear about notation everywhere"),
              _pt(1, "clarity", "paper one also has typos throughout the draft"),
              _pt(2, "clarity", "paper two has confusing figures and captions")]
    patterns = aggregate_by_aspect(points, total_papers=2, min_papers=1)
    examples = patterns[0].examples
    # 서로 다른 논문에서 예시를 뽑아야 함 (논문1 하나 + 논문2 하나)
    assert len(examples) == 2
    assert {e.paper_id for e in examples} == {1, 2}


def test_examples_carry_source_point_id():
    """근거 추적의 최소 단위. 없으면 요약의 인용을 원문까지 되짚을 수 없다."""
    points = [{"point_id": 777, "paper_id": 1, "aspect": "clarity",
               "text": "notation is inconsistent across sections"}]
    patterns = aggregate_by_aspect(points, total_papers=1, min_papers=1)
    assert patterns[0].examples[0].review_point_id == 777


def test_examples_tolerate_missing_point_id():
    """point_id 없이 부르는 경로(테스트/폴백)도 깨지지 않아야 한다."""
    patterns = aggregate_by_aspect([_pt(1, "clarity", "unclear notation here")],
                                   total_papers=1, min_papers=1)
    assert patterns[0].examples[0].review_point_id is None


# ---------------------------------------------------------- 통계 유틸

def test_binom_tail_edges():
    # 전 구간 합이라 부동소수 오차가 남는다 (1.0 대신 0.999...) — 허용오차로 본다
    assert binom_tail(0, 10, 0.3, upper=True) == pytest.approx(1.0)   # P(X>=0)=1
    assert binom_tail(10, 10, 0.5, upper=False) == pytest.approx(1.0)  # P(X<=n)=1
    assert binom_tail(5, 0, 0.5) == 1.0                   # n=0 → 무정보
    assert binom_tail(5, 10, 0.0) == 1.0                  # p 경계 → 무정보


def test_binom_tail_matches_hand_computation():
    # P(X>=2), n=3, p=0.5 → (3+1)/8 = 0.5
    assert abs(binom_tail(2, 3, 0.5) - 0.5) < 1e-12


def test_binom_tail_upper_is_small_for_extreme_excess():
    # base rate 20%인데 20편 중 18편 → 극히 드묾
    assert binom_tail(18, 20, 0.2) < 1e-8


def test_fisher_detects_clear_separation():
    # 지적받은 10편 전부 탈락, 미지적 10편 전부 통과 → 강하게 유의
    assert fisher_exact_less(0, 10, 10, 0) < 0.001


def test_fisher_not_significant_on_tiny_sample():
    # "4편 중 0편 통과"는 그럴듯해 보이지만 표본이 작아 유의하지 않다
    assert fisher_exact_less(0, 4, 8, 8) > 0.05


def test_fisher_degenerate_table_returns_one():
    assert fisher_exact_less(0, 0, 5, 5) == 1.0      # with 그룹이 빔
    assert fisher_exact_less(3, 3, 0, 0) == 1.0      # without 그룹이 빔


# ------------------------------------------------- lift / base rate

_BASE = {"baselines": 0.788, "novelty": 0.414, "reproducibility": 0.225}


def test_lift_computed_against_base_rate():
    # 20편 중 17편 baselines = 85%, base rate 78.8% → lift 1.08 (평범)
    points = [_pt(i, "baselines") for i in range(17)]
    patterns = aggregate_by_aspect(points, total_papers=20, base_rates=_BASE)
    p = patterns[0]
    assert p.base_rate == 0.788
    assert abs(p.lift - 1.08) < 0.01
    assert not p.is_distinctive          # 흔한 지적은 두드러짐이 아니다


def test_distinctive_requires_both_lift_and_significance():
    # 20편 중 16편 reproducibility = 80%, base rate 22.5% → lift 3.6, p 극소
    points = [_pt(i, "reproducibility") for i in range(16)]
    patterns = aggregate_by_aspect(points, total_papers=20, base_rates=_BASE)
    assert patterns[0].is_distinctive
    assert patterns[0].lift > 3

    # 같은 lift 방향이어도 표본이 작으면 유의하지 않아 두드러짐이 아니다
    small = [_pt(i, "reproducibility") for i in range(2)]
    patterns = aggregate_by_aspect(small, total_papers=3, base_rates=_BASE,
                                   min_papers=2)
    assert not patterns[0].is_distinctive


def test_distinctive_sorts_above_more_frequent_but_common_aspect():
    # baselines가 더 많이 등장하지만(17편) 코퍼스 평균 수준,
    # reproducibility는 적게 등장해도(12편) base rate 대비 압도적
    points = ([_pt(i, "baselines") for i in range(17)] +
              [_pt(i, "reproducibility") for i in range(12)])
    patterns = aggregate_by_aspect(points, total_papers=20, base_rates=_BASE)
    assert patterns[0].aspect == "reproducibility"
    assert patterns[0].paper_count < patterns[1].paper_count   # 빈도는 더 적다


def test_no_base_rates_falls_back_to_frequency_order():
    points = ([_pt(i, "baselines") for i in range(5)] +
              [_pt(i, "novelty") for i in range(2)])
    patterns = aggregate_by_aspect(points, total_papers=5, min_papers=2)
    assert patterns[0].aspect == "baselines"
    assert all(p.lift is None and not p.is_distinctive for p in patterns)


# ------------------------------------------------------- 당락 대조

def _decisions(accepted: set, rejected: set):
    return ({pid: "accept-poster" for pid in accepted} |
            {pid: "reject" for pid in rejected})


def test_accept_contrast_splits_by_criticism():
    # 논문 0,1은 novelty 지적받고 탈락 / 2,3은 지적 없고 통과
    points = [_pt(0, "novelty"), _pt(1, "novelty")]
    patterns = aggregate_by_aspect(
        points, total_papers=4, decisions=_decisions({2, 3}, {0, 1}),
        all_paper_ids={0, 1, 2, 3})
    p = patterns[0]
    assert (p.accept_with, p.decided_with) == (0, 2)
    assert (p.accept_without, p.decided_without) == (2, 2)
    assert p.accept_rate_with == 0.0 and p.accept_rate_without == 1.0


def test_contrast_ignores_unknown_decisions():
    points = [_pt(0, "novelty"), _pt(1, "novelty")]
    decisions = {0: "accept-poster", 1: "unknown", 2: "reject", 3: "unknown"}
    patterns = aggregate_by_aspect(points, total_papers=4, decisions=decisions,
                                   all_paper_ids={0, 1, 2, 3})
    p = patterns[0]
    assert p.decided_with == 1 and p.decided_without == 1   # unknown 2편 제외


def test_withdrawn_counts_as_not_accepted():
    points = [_pt(0, "novelty"), _pt(1, "novelty")]
    decisions = {0: "withdrawn", 1: "withdrawn", 2: "accept-oral", 3: "accept-poster"}
    patterns = aggregate_by_aspect(points, total_papers=4, decisions=decisions,
                                   all_paper_ids={0, 1, 2, 3})
    assert patterns[0].accept_rate_with == 0.0


def test_contrast_significance_needs_enough_evidence():
    # 큰 표본에서 뚜렷한 격차 → 유의
    points = [_pt(i, "novelty") for i in range(10)]
    patterns = aggregate_by_aspect(
        points, total_papers=20, decisions=_decisions(set(range(10, 20)),
                                                      set(range(10))),
        all_paper_ids=set(range(20)))
    assert patterns[0].is_contrast_significant

    # 작은 표본의 같은 방향 격차 → 유의하지 않음
    points = [_pt(0, "novelty"), _pt(1, "novelty")]
    patterns = aggregate_by_aspect(
        points, total_papers=4, decisions=_decisions({2, 3}, {0, 1}),
        all_paper_ids={0, 1, 2, 3})
    assert not patterns[0].is_contrast_significant


def test_no_decisions_leaves_contrast_empty():
    points = [_pt(0, "novelty"), _pt(1, "novelty")]
    patterns = aggregate_by_aspect(points, total_papers=4)
    assert patterns[0].accept_rate_with is None
    assert patterns[0].contrast_p_value is None
    assert not patterns[0].is_contrast_significant


def test_papers_without_any_criticism_stay_in_denominator():
    # 논문 2,3은 지적항목이 아예 없다 — all_paper_ids로 대조군에 포함돼야 한다
    points = [_pt(0, "novelty"), _pt(1, "novelty")]
    patterns = aggregate_by_aspect(
        points, total_papers=4, decisions=_decisions({2, 3}, {0, 1}),
        all_paper_ids={0, 1, 2, 3})
    assert patterns[0].decided_without == 2

    # all_paper_ids 없으면 points에 등장한 논문만 대조군이 된다
    patterns = aggregate_by_aspect(
        points, total_papers=4, decisions=_decisions({2, 3}, {0, 1}))
    assert patterns[0].decided_without == 0


def test_split_format_reviews_are_preferred_as_examples():
    """미분리 리뷰 문장은 대표 '지적'으로 뽑히면 안 된다.

    미분리 리뷰(2023년 이전)는 본문 전체가 weakness로 라벨링돼 있어 요약·칭찬이
    섞인다. 길이만으로 고르면 "This paper proposes ..." 같은 요약문이 지적으로
    인용된다(실측). 집계 수치는 그대로 두고 인용 문장만 신뢰할 수 있는 쪽을 쓴다.
    """
    points = [
        {"point_id": 1, "paper_id": 1, "aspect": "baselines",
         "text": "This paper proposes a very long summary sentence " * 5,
         "from_unsplit": True},
        {"point_id": 2, "paper_id": 2, "aspect": "baselines",
         "text": "only two baselines compared", "from_unsplit": False},
    ]
    patterns = aggregate_by_aspect(points, total_papers=2, min_papers=1)
    first = patterns[0].examples[0]
    assert first.review_point_id == 2          # 짧아도 분리 포맷이 먼저
    assert first.from_unsplit_review is False


def test_unsplit_example_is_used_when_nothing_else_exists_but_is_flagged():
    points = [{"point_id": 9, "paper_id": 1, "aspect": "clarity",
               "text": "the whole review body", "from_unsplit": True}]
    patterns = aggregate_by_aspect(points, total_papers=1, min_papers=1)
    assert patterns[0].examples[0].from_unsplit_review is True
