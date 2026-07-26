"""리뷰 지적항목 집계·클러스터링 로직 테스트 (모델/DB 불필요, numpy만)."""
import numpy as np
import pytest

from paper_assistant.graph.clustering import (
    _greedy_cluster, aggregate_by_aspect, binom_tail, fisher_exact_less)


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


def _norm(v):
    v = np.asarray(v, dtype=float)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def test_two_tight_groups_separate():
    # 두 방향으로 뚜렷이 갈리는 벡터 → 2개 클러스터
    vecs = _norm([[1, 0, 0], [0.98, 0.02, 0], [0.97, 0.05, 0],   # 그룹 A
                  [0, 1, 0], [0.02, 0.98, 0]])                    # 그룹 B
    clusters = _greedy_cluster(vecs, threshold=0.9)
    assert len(clusters) == 2
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [2, 3]


def test_all_similar_one_cluster():
    vecs = _norm([[1, 0], [0.99, 0.01], [0.98, 0.02]])
    clusters = _greedy_cluster(vecs, threshold=0.8)
    assert len(clusters) == 1
    assert len(clusters[0]) == 3


def test_all_dissimilar_singletons():
    vecs = _norm([[1, 0, 0], [0, 1, 0], [0, 0, 1]])
    clusters = _greedy_cluster(vecs, threshold=0.5)
    assert len(clusters) == 3
    assert all(len(c) == 1 for c in clusters)


def test_every_point_assigned_exactly_once():
    rng = np.random.default_rng(0)
    vecs = _norm(rng.normal(size=(30, 8)))
    clusters = _greedy_cluster(vecs, threshold=0.6)
    flat = [i for c in clusters for i in c]
    assert sorted(flat) == list(range(30))   # 빠짐/중복 없음


def test_high_degree_point_seeds_first():
    # 0번이 1,2,3과 모두 유사, 4번은 고립 → 0이 큰 클러스터 시드
    vecs = _norm([[1, 0], [0.99, 0.01], [0.98, 0.02], [0.97, 0.03], [-1, 0]])
    clusters = _greedy_cluster(vecs, threshold=0.9)
    biggest = max(clusters, key=len)
    assert 0 in biggest and len(biggest) == 4
