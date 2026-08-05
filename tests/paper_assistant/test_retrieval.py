"""RRF 결합·가중합 재정렬 로직 테스트 (DB 불필요)."""
import pytest

from paper_assistant.retrieval.hybrid_search import (
    CANDIDATE_POOL, HNSW_EF_SEARCH, NEUTRAL_CITATION_PERCENTILE,
    RECENCY_HALF_LIFE, RRF_K, W_CITATION, W_RECENCY, W_SIMILARITY,
    _ef_search_for, match_type, max_rrf, recency_score, rerank, rrf_fuse,
    weighted_score)


def test_match_type_reflects_which_retrievers_hit():
    assert match_type(1, 1) == "both"        # 의미+용어
    assert match_type(3, None) == "semantic"  # 임베딩만
    assert match_type(None, 3) == "lexical"   # 용어만


def test_match_type_treats_rank_zero_as_a_hit():
    """순위는 1부터 매기지만, 0이 들어와도 '못 찾음'으로 오해하면 안 된다."""
    assert match_type(0, None) == "semantic"
    assert match_type(None, 0) == "lexical"


def test_ef_search_covers_candidate_pool():
    """HNSW는 ef_search보다 많은 행을 못 준다 — pool보다 반드시 커야 한다.

    기본값 40 < CANDIDATE_POOL 50이라 벡터 후보가 잘리던 버그가 있었다 (§20).
    """
    assert HNSW_EF_SEARCH >= CANDIDATE_POOL


@pytest.mark.parametrize("limit", [10, 50, 150, 300])
def test_ef_search_follows_the_requested_limit(limit):
    """ef_search가 **그때의 limit**을 따라야 한다 — 모듈 상수에 고정되면 안 된다.

    hybrid_search(pool=...)로 풀을 키웠는데 ef_search가 기본 풀 기준에 머물면,
    HNSW가 요청한 개수를 못 채워 벡터 후보만 조용히 잘린다. 에러가 안 나서
    알아채기 어려운 종류의 버그다.
    """
    assert _ef_search_for(limit) >= limit


def test_both_retrievers_agreeing_beats_either_alone():
    """양쪽 검색기에 모두 잡힌 문서가 한쪽에서만 1위인 문서를 이겨야 한다.

    이것이 하이브리드를 쓰는 이유 그 자체다.
    """
    vector = {100: 1, 200: 2}   # 벡터 1위=100
    fts = {200: 1, 300: 2}      # FTS 1위=200
    scores = rrf_fuse(vector, fts)
    # 200은 양쪽에 잡힘(2위+1위), 100은 벡터에서만 1위
    assert scores[200] > scores[100]
    assert scores[200] > scores[300]


def test_rank_one_scores_higher_than_rank_two():
    scores = rrf_fuse({10: 1, 20: 2, 30: 3})
    assert scores[10] > scores[20] > scores[30]


def test_missing_from_one_retriever_still_scores():
    """한쪽에만 있어도 탈락하지 않는다 (OR 결합이지 AND가 아니다)."""
    scores = rrf_fuse({1: 1}, {2: 1})
    assert 1 in scores and 2 in scores
    assert scores[1] == scores[2]


def test_k_dampens_rank_differences():
    """k가 클수록 상위 순위 간 점수 차이가 완만해진다."""
    tight = rrf_fuse({1: 1, 2: 10}, k=60)
    loose = rrf_fuse({1: 1, 2: 10}, k=1)
    assert (loose[1] / loose[2]) > (tight[1] / tight[2])


def test_empty_input_returns_empty():
    assert rrf_fuse() == {}
    assert rrf_fuse({}, {}) == {}


# ------------------------------------------------------ 가중합 재정렬

def test_weights_sum_to_one():
    """합이 1.0이라야 각 가중치를 '지분 비율'로 읽을 수 있다 — 설계의 전제다."""
    assert W_SIMILARITY + W_RECENCY + W_CITATION == pytest.approx(1.0)


def test_recency_beats_similarity_in_the_weights():
    """교수님 요구사항: 최신성이 유사도보다 중요하다. 가중치에 그대로 박혀야 한다."""
    assert W_RECENCY > W_SIMILARITY


def test_recency_decays_by_half_life():
    """반감기만큼 묵으면 절반, 두 배면 1/4.

    half_life를 명시로 넘긴다 — 튜닝 상수(RECENCY_HALF_LIFE)를 바꿨다고 해서
    감쇠 공식 자체의 테스트가 깨지면 안 되기 때문이다.
    """
    assert recency_score(2025, 2025, half_life=2) == pytest.approx(1.0)
    assert recency_score(2023, 2025, half_life=2) == pytest.approx(0.5)
    assert recency_score(2021, 2025, half_life=2) == pytest.approx(0.25)


def test_half_life_defaults_to_the_tuned_constant():
    """기본 인자로 박아두면 상수를 바꿔도 반영되지 않는다 — 그 함정을 막는다."""
    assert recency_score(2024, 2025) == pytest.approx(
        recency_score(2024, 2025, half_life=RECENCY_HALF_LIFE))


def test_recency_is_monotonic_in_year():
    scores = [recency_score(y, 2025) for y in range(2020, 2026)]
    assert scores == sorted(scores)


def test_recency_clamps_years_newer_than_the_corpus():
    """코퍼스보다 미래인 연도가 들어와도 1.0을 넘지 않는다 (음수 지수 방지)."""
    assert recency_score(2027, 2025) == pytest.approx(1.0)


def test_max_rrf_matches_both_retrievers_ranking_first():
    """정규화 분모는 '모든 검색기에서 1위'일 때의 RRF 값이어야 한다."""
    assert max_rrf(2) == pytest.approx(rrf_fuse({1: 1}, {1: 1})[1])


def test_newest_paper_wins_over_the_most_similar_one():
    """**이 설계의 핵심 요구사항.** 유사도 만점 구논문 vs 유사도 최하 최신논문.

    2020년 논문이 양쪽 검색기 1위(유사도 만점)여도, 2025년 논문이 이겨야 한다.
    이게 뒤집히면 "최신성이 유사도보다 우선"이 지켜지지 않은 것이다.
    """
    best = max_rrf(2)                      # 양쪽 1위
    worst = 1.0 / (RRF_K + CANDIDATE_POOL)  # 한쪽 검색기 꼴찌
    signals = rerank(
        {1: best, 2: worst},
        {1: (2020, 0.5), 2: (2025, 0.5)},   # 인용도는 동일하게 고정
        max_year=2025)
    assert signals[2].final > signals[1].final


def test_same_year_papers_are_ordered_by_similarity():
    """최신성이 같으면 유사도가 순위를 정한다 — 유사도를 버리는 설계가 아니다."""
    signals = rerank(
        {1: max_rrf(2), 2: 1.0 / (RRF_K + 10)},
        {1: (2025, 0.5), 2: (2025, 0.5)},
        max_year=2025)
    assert signals[1].final > signals[2].final


def test_citation_breaks_ties_between_identical_papers():
    """유사도·최신성이 같으면 인용 백분위가 갈라야 한다."""
    signals = rerank(
        {1: max_rrf(2), 2: max_rrf(2)},
        {1: (2025, 0.95), 2: (2025, 0.10)},
        max_year=2025)
    assert signals[1].final > signals[2].final


def test_missing_citation_percentile_is_neutral_not_zero():
    """결측(코퍼스의 30.5%)을 0으로 처리하면 결측률이 높은 2025년이 손해를 본다.

    NULL은 '인용 0회'가 아니라 'S2 보강이 닿지 않았다'는 뜻이므로 중립값이어야 한다.
    """
    signals = rerank({1: max_rrf(2)}, {1: (2025, None)}, max_year=2025)
    assert signals[1].citation == NEUTRAL_CITATION_PERCENTILE

    known_zero = rerank({1: max_rrf(2)}, {1: (2025, 0.0)}, max_year=2025)
    assert signals[1].final > known_zero[1].final


def test_rerank_survives_papers_missing_from_metadata():
    """조회 사이에 사라진 논문 때문에 검색 전체가 죽으면 안 된다."""
    signals = rerank({999: max_rrf(2)}, {}, max_year=2025)
    assert 999 in signals
    assert signals[999].citation == NEUTRAL_CITATION_PERCENTILE


def test_similarity_is_normalized_to_unit_range():
    """유사도 성분은 [0,1]이어야 가중치가 지분으로서 의미를 갖는다."""
    signals = rerank(
        {1: max_rrf(2), 2: 1.0 / (RRF_K + CANDIDATE_POOL)},
        {1: (2025, 0.5), 2: (2025, 0.5)},
        max_year=2025)
    assert signals[1].similarity == pytest.approx(1.0)
    assert 0.0 < signals[2].similarity < 1.0


def test_weighted_score_is_bounded_by_zero_and_one():
    assert weighted_score(0.0, 0.0, 0.0) == pytest.approx(0.0)
    assert weighted_score(1.0, 1.0, 1.0) == pytest.approx(1.0)
