"""RRF 결합·가중합 재정렬 로직 테스트 (DB 불필요)."""
import inspect

import pytest

from paper_assistant.retrieval import hybrid_search
from paper_assistant.retrieval.hybrid_search import (
    CANDIDATE_POOL, HNSW_EF_SEARCH, NEUTRAL_CITATION_PERCENTILE, PRESET_NAMES,
    RERANK_CANDIDATES, RRF_K, W_CITATION, W_RECENCY, W_SIMILARITY,
    _ef_search_for, balanced_weights, match_type, max_rrf, ranking_weights,
    recency_score, rerank, rrf_fuse, weighted_score)


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


def test_half_life_is_read_at_call_time_not_bound_at_definition(monkeypatch):
    """상수를 바꾸면 함수가 따라와야 한다 — 기본 인자에 박히는 함정을 막는다.

    `def recency_score(..., half_life=RECENCY_HALF_LIFE)` 로 쓰면 파이썬이 정의
    시점에 값을 한 번 평가해 박아버린다. 그러면 튜닝 실험이 모듈 상수를 바꿔도
    반영되지 않아 **조용히 옛 값으로 계산된다** (실제로 겪은 문제다).

    상수를 실제로 바꿔서 검증해야 한다. 상수를 안 바꾸고 두 호출을 비교하면
    버그가 있는 버전에서도 값이 같아 그냥 통과한다 — 못 막는 테스트가 된다.
    """
    monkeypatch.setattr(hybrid_search, "RECENCY_HALF_LIFE", 6.0)
    # 반감기 6년이면 6년 묵은 논문이 정확히 절반이 된다.
    assert hybrid_search.recency_score(2019, 2025) == pytest.approx(0.5)


@pytest.mark.parametrize("param", ["pool", "top_k"])
def test_search_knobs_are_read_at_call_time_too(param):
    """hybrid_search 의 pool·top_k 도 같은 함정을 피해야 한다.

    DB 없이 확인할 수 있는 지점은 기본값이 None 인지다. `= CANDIDATE_POOL` /
    `= RERANK_CANDIDATES` 로 되돌리는 순간 상수 변경이 반영되지 않는다.
    """
    default = inspect.signature(hybrid_search.hybrid_search).parameters[param].default
    assert default is None


def test_pool_is_at_least_double_the_candidates_returned():
    """재정렬은 후보 풀 위에서만 일어난다 — 풀이 좁으면 상위 절반을 넘길 수 없다.

    RERANK_CANDIDATES 를 올릴 때 CANDIDATE_POOL 을 같이 올리는 것을 잊으면, 반환
    목록의 뒤쪽이 사실상 풀 하위로 채워진다. 에러가 안 나서 알아채기 어렵다.
    """
    assert CANDIDATE_POOL >= RERANK_CANDIDATES * 2


def test_both_retrievers_filter_on_reviews_identically():
    """리뷰 보유 조건을 한쪽 검색기에만 걸면 RRF 결합이 조용히 왜곡된다.

    한쪽만 필터링하면 그쪽 순위표만 짧아져, 걸러진 논문 뒤에 있던 문서들의 순위가
    반대쪽 대비 앞당겨진다. 점수가 틀리지만 에러는 안 난다. 두 쿼리가 **같은**
    조건 문자열을 참조하는지로 검증한다.
    """
    src = inspect.getsource(hybrid_search)
    vector_sql = inspect.getsource(hybrid_search._vector_search)
    fts_sql = inspect.getsource(hybrid_search._fulltext_search)
    assert "_HAS_REVIEWS" in vector_sql
    assert "_HAS_REVIEWS" in fts_sql
    # 조건이 실제로 reviews 를 보는지 (상수만 있고 내용이 바뀐 경우를 잡는다)
    assert "FROM reviews" in src and "EXISTS" in hybrid_search._HAS_REVIEWS


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


# ------------------------------------------- 사용자 선호별 랭킹 프리셋
#
# 온보딩 "최신 논문과 인용이 많은 논문, 뭘 우선할까요?"가 여기로 들어온다.
# 프리셋이 늘거나 숫자가 바뀌면 아래 테스트가 **자동으로** 따라온다 —
# PRESET_NAMES와 ranking_weights를 그대로 읽기 때문이다.


@pytest.mark.parametrize("preset", PRESET_NAMES)
def test_every_preset_is_a_valid_share_split(preset):
    """합이 1.0이어야 각 값을 지분으로 읽을 수 있다 — 기본 프리셋만의 전제가 아니다."""
    w = ranking_weights(preset)
    assert w.similarity + w.recency + w.citation == pytest.approx(1.0)
    assert w.half_life > 0


@pytest.mark.parametrize("preset", PRESET_NAMES)
def test_no_preset_puts_similarity_above_recency(preset):
    """**요구사항은 사용자 선호보다 상위다** (docs/랭킹_가중치_설계.md §1).

    "인용 많은 논문 우선"을 고른 사용자에게도 최신성 > 유사도는 유지된다. 인용도에
    줄 지분은 최신성이 아니라 유사도에서 가져와야 한다는 뜻이다 — 최신성에서 빼면
    아래 테스트가 즉시 깨진다.
    """
    w = ranking_weights(preset)
    assert w.recency > w.similarity


@pytest.mark.parametrize("preset", PRESET_NAMES)
def test_no_preset_breaks_the_recency_requirement(preset):
    """test_newest_paper_wins_over_the_most_similar_one을 **모든 프리셋에** 건다.

    상수 한 벌만 검사하면, 프리셋이 늘어난 뒤로는 기본값만 지켜지고 나머지는
    아무도 안 보는 상태가 된다. 여유(margin)는 CANDIDATE_POOL에 묶여 있으므로
    (hybrid_search의 ⚠️ 참고) 풀을 키울 때 가장 빠듯한 프리셋부터 여기서 깨진다.
    """
    best = max_rrf(2)                        # 양쪽 검색기 1위 = 유사도 만점
    worst = 1.0 / (RRF_K + CANDIDATE_POOL)   # 후보 풀 꼴찌
    signals = rerank(
        {1: best, 2: worst},
        {1: (2020, 0.5), 2: (2025, 0.5)},    # 인용도는 동일하게 고정
        max_year=2025, weights=ranking_weights(preset))
    assert signals[2].final > signals[1].final


def test_unknown_preference_falls_back_to_the_default_preset():
    """값의 출처가 무인증 엔드포인트다 — 모르는 문자열에 계산을 맡길 수 없다."""
    assert ranking_weights(None) == balanced_weights()
    assert ranking_weights("") == balanced_weights()
    assert ranking_weights("가장 인용 많은 논문만 주세요") == balanced_weights()


def test_the_default_preset_reads_module_constants_at_call_time(monkeypatch):
    """프리셋 표에 상수를 미리 박으면 recency_score가 피한 함정에 그대로 빠진다.

    모듈 로드 시점에 값이 한 번 복사되어, 튜닝 실험이 W_*를 바꿔도 조용히 옛
    값으로 계산된다.
    """
    monkeypatch.setattr(hybrid_search, "W_RECENCY", 0.99)
    monkeypatch.setattr(hybrid_search, "RECENCY_HALF_LIFE", 9.0)
    w = hybrid_search.balanced_weights()
    assert w.recency == 0.99
    assert w.half_life == 9.0


def test_recent_preset_favours_the_new_paper_where_the_default_does_not():
    """프리셋이 이름만 다르고 결과가 같으면 온보딩 질문이 거짓말이 된다.

    유사도 만점 2023년 vs 유사도 꼴찌 2025년 — 기본값은 옛 논문을 남기지만
    '최신 트렌드'를 고른 사용자에게는 최신 논문이 올라와야 한다.
    """
    scores = {1: max_rrf(2), 2: 1.0 / (RRF_K + CANDIDATE_POOL)}
    fields = {1: (2023, 0.5), 2: (2025, 0.5)}
    default = rerank(scores, fields, 2025, weights=ranking_weights("balanced"))
    recent = rerank(scores, fields, 2025, weights=ranking_weights("recent"))
    assert default[1].final > default[2].final
    assert recent[2].final > recent[1].final


def test_cited_preset_favours_the_well_cited_paper_over_the_newer_one():
    """'검증된(인용 많은) 논문'의 약속. 이게 안 뒤집히면 그 선택지는 장식이다.

    2023년 인용 상위(0.95) vs 2025년 인용 하위(0.30), 유사도는 동일.
    """
    scores = {1: max_rrf(2), 2: max_rrf(2)}
    fields = {1: (2023, 0.95), 2: (2025, 0.30)}
    default = rerank(scores, fields, 2025, weights=ranking_weights("balanced"))
    cited = rerank(scores, fields, 2025, weights=ranking_weights("cited"))
    assert default[2].final > default[1].final     # 기본값은 최신 쪽
    assert cited[1].final > cited[2].final         # 선호를 고르면 뒤집힌다


def test_cited_preset_spreads_the_years_wider_than_the_default():
    """인용도 가중치만 올리면 거의 아무 일도 안 일어난다 (RankingWeights 주석).

    citation_percentile이 **같은 연도 안**의 백분위이고 2025년은 58.0%가 결측
    (=중립 0.5)이라, 연도가 퍼지지 않으면 비교 자체가 성립하지 않는다. 반감기가
    프리셋에 함께 묶여 있어야 하는 이유이므로 테스트로 고정한다.
    """
    assert ranking_weights("cited").half_life > balanced_weights().half_life
    assert ranking_weights("recent").half_life < balanced_weights().half_life


def test_weights_are_read_at_call_time_by_the_search_entry_point():
    """hybrid_search(weights=...)의 기본값도 None이어야 한다 (pool·top_k와 같은 이유)."""
    default = inspect.signature(hybrid_search.hybrid_search).parameters["weights"].default
    assert default is None


# ------------------------------------------------ 코퍼스 중복 접기
#
# 코퍼스에 같은 논문이 두 행으로 들어 있다 (NeurIPS 2021 301쌍). openreview_id도
# forum_id도 다른 별개의 노트라 paper_id 기준 중복 제거로는 안 걸리고, 실측에서
# 표본 8쌍 중 6쌍이 후보 50편에 나란히 들어왔다.

def _sig(final):
    from paper_assistant.retrieval.hybrid_search import RankingSignals
    return RankingSignals(final=final, similarity=final, recency=1.0, citation=0.5)


def test_duplicate_papers_are_folded_into_one():
    """같은 (제목, venue)가 후보에 두 번 들어가면 자리가 낭비되고, LLM이 둘 다 고르면
    사용자에게 같은 논문이 두 번 뜬다 (paper_id가 달라 선정 단계 중복 제거도 통과)."""
    ranked = [(1, _sig(0.9)), (2, _sig(0.8)), (3, _sig(0.7))]
    dedupe = {1: ("같은 논문", "NeurIPS 2021", 3),
              2: ("같은 논문", "NeurIPS 2021", 7),
              3: ("다른 논문", "NeurIPS 2021", 4)}
    out = hybrid_search._pick_without_duplicates(ranked, dedupe, top_k=10)
    assert [pid for pid, _ in out] == [2, 3], "중복이 접히지 않았다"


def test_the_twin_with_more_reviews_wins():
    """어차피 한 쪽만 보여준다면 읽을 것이 많은 편이 낫다.

    두 행의 리뷰는 **하나도 겹치지 않으므로**(301쌍 전부) 어느 쪽을 고르냐가 곧
    사용자가 볼 리뷰 수다.
    """
    ranked = [(1, _sig(0.9)), (2, _sig(0.8))]
    dedupe = {1: ("t", "NeurIPS 2021", 3), 2: ("t", "NeurIPS 2021", 7)}
    assert [pid for pid, _ in
            hybrid_search._pick_without_duplicates(ranked, dedupe, 10)] == [2]


def test_folding_keeps_the_higher_rank_position():
    """리뷰가 많은 쪽으로 바꾸더라도 **자리는 높은 순위 자리**여야 한다."""
    ranked = [(1, _sig(0.9)), (9, _sig(0.85)), (2, _sig(0.8))]
    dedupe = {1: ("t", "N 2021", 3), 2: ("t", "N 2021", 7), 9: ("u", "N 2021", 1)}
    assert [pid for pid, _ in
            hybrid_search._pick_without_duplicates(ranked, dedupe, 10)] == [2, 9]


def test_same_title_at_different_venues_is_not_folded():
    """ICLR 2024 → NeurIPS 2024 재투고는 같은 제목이지만 **다른 심사**다.

    접어 버리면 "이 논문은 떨어졌다가 고쳐서 붙었다"를 보여줄 수 없게 된다
    (submission_links가 추적하는 바로 그 관계다).
    """
    ranked = [(1, _sig(0.9)), (2, _sig(0.8))]
    dedupe = {1: ("t", "ICLR 2024", 4), 2: ("t", "NeurIPS 2024", 4)}
    assert len(hybrid_search._pick_without_duplicates(ranked, dedupe, 10)) == 2


def test_folding_happens_before_slicing_not_after():
    """자른 뒤에 접으면 요청한 개수보다 적게 나간다 — 조용히 후보가 줄어든다."""
    ranked = [(1, _sig(0.9)), (2, _sig(0.8)), (3, _sig(0.7))]
    dedupe = {1: ("a", "v", 1), 2: ("a", "v", 9), 3: ("b", "v", 1)}
    out = hybrid_search._pick_without_duplicates(ranked, dedupe, top_k=2)
    assert len(out) == 2, "중복을 접고도 요청한 2편을 채워야 한다"


def test_papers_missing_from_metadata_are_not_folded_together():
    """조회 사이에 사라진 논문들이 같은 키로 묶여 서로를 지우면 안 된다."""
    ranked = [(1, _sig(0.9)), (2, _sig(0.8))]
    assert len(hybrid_search._pick_without_duplicates(ranked, {}, 10)) == 2
