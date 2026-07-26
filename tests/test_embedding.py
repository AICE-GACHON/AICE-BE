"""임베딩 유틸 테스트 (모델 로드 없이 순수 함수만 — CI에서 빠르게 돌도록)."""
import pytest

from paper_assistant.embedding.specter2 import (
    retrieval_confidence, similarity_percentile)


def test_percentile_matches_measured_reference_points():
    """scripts/measure_similarity_dist.py 실측값과 일치해야 한다."""
    assert similarity_percentile(0.8448) == pytest.approx(50.0, abs=0.1)
    assert similarity_percentile(0.9231) == pytest.approx(99.0, abs=0.1)
    assert similarity_percentile(0.7924) == pytest.approx(5.0, abs=0.1)


def test_percentile_is_monotonic():
    scores = [0.70, 0.75, 0.80, 0.85, 0.90, 0.93, 0.96, 0.99]
    pcts = [similarity_percentile(s) for s in scores]
    assert pcts == sorted(pcts)


def test_percentile_stays_in_range():
    for s in (0.0, 0.5, 0.72, 0.845, 0.95, 1.0):
        assert 0.0 <= similarity_percentile(s) <= 100.0


def test_unrelated_pair_lands_near_median():
    """무관한 논문쌍의 실측 코사인 0.844는 중앙값 근처여야 한다.

    이 값이 '84% 유사'로 오해되는 것을 막는 것이 이 함수의 존재 이유.
    """
    assert 40 < similarity_percentile(0.8443) < 60


def test_strongly_related_pair_is_top_percentile():
    """Transformer/BERT 쌍(0.9202), tabular 쌍(0.9563)의 실측값."""
    assert similarity_percentile(0.9202) > 95
    assert similarity_percentile(0.9563) > 99


def test_percentile_saturates_over_search_results():
    """검색 top-20 구간에서는 백분위가 사실상 한 점에 뭉친다 — 논문별 표시에 못 쓴다.

    이 포화가 §20에서 similarity_percentile을 Report에서 제거한 근거다.
    실측 top-20 코사인(GNN 쿼리)은 0.9378~0.9510으로 폭이 0.013뿐이고,
    백분위로 바꾸면 1위와 20위가 1%p 안쪽으로 붙어 순위를 구분할 수 없다.
    """
    top20 = [0.9510, 0.9442, 0.9411, 0.9400, 0.9378]
    pcts = [similarity_percentile(c) for c in top20]
    assert min(pcts) > 99.0
    assert max(pcts) - min(pcts) < 1.0


# ------------------------------------------------------ 검색 신뢰도 판정

def test_confidence_strong_for_in_domain_measurements():
    """실측 도메인 안 쿼리의 top-5 코사인 (GNN / LoRA)."""
    assert retrieval_confidence(
        [0.9517, 0.9460, 0.9450, 0.9440, 0.9378])[0] == "strong"
    assert retrieval_confidence(
        [0.9724, 0.9680, 0.9660, 0.9640, 0.9617])[0] == "strong"


def test_confidence_weak_for_out_of_domain_measurements():
    """실측 도메인 밖 쿼리 (치즈 미생물학 / 한자동맹)."""
    assert retrieval_confidence(
        [0.8708, 0.8600, 0.8560, 0.8540, 0.8522])[0] == "weak"
    assert retrieval_confidence(
        [0.8630, 0.8610, 0.8600, 0.8590, 0.8570])[0] == "weak"


def test_confidence_moderate_between_thresholds():
    assert retrieval_confidence([0.915] * 5)[0] == "moderate"


def test_confidence_uses_only_top_k():
    # 상위 5개만 본다 — 꼬리가 아무리 낮아도 판정이 바뀌면 안 된다
    level, _ = retrieval_confidence([0.96] * 5 + [0.10] * 50)
    assert level == "strong"


def test_confidence_returns_mean_of_top_k_as_evidence():
    _level, evidence = retrieval_confidence([1.0, 0.9, 0.8, 0.7, 0.6, 0.0])
    assert evidence == pytest.approx(0.8)


def test_confidence_handles_empty_and_missing():
    assert retrieval_confidence([]) == ("weak", 0.0)
    assert retrieval_confidence([None, None]) == ("weak", 0.0)


def test_confidence_survives_fewer_than_k_results():
    level, evidence = retrieval_confidence([0.96, 0.95])
    assert level == "strong" and evidence == pytest.approx(0.955)
