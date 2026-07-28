"""검색 신뢰도 판정 테스트 (모델 로드 없이 순수 함수만 — CI에서 빠르게 돌도록)."""
import pytest

from paper_assistant.embedding.specter2 import retrieval_confidence


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
