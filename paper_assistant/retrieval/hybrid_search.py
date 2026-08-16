"""하이브리드 검색: SPECTER2 벡터 + Postgres full-text, RRF로 결합 후 재정렬.

**이 모듈은 2단계 추천의 1단계다.** 여기서 뽑은 후보를 LLM이 입력 PDF 원문과 대조해
최종 5편을 고른다(graph/nodes.py의 llm_rerank). 따라서 이 층의 책임은 **정밀도가 아니라
재현율**이다 — "충분히 비슷한 대역"을 넓게 확보해서 판정자에게 넘기는 것이 목적이고,
그 안에서 누가 더 비슷한지를 가리는 일은 여기서 하지 않는다(할 수도 없다, 아래 참고).

왜 하이브리드인가 (§11.2 실측 근거):
SPECTER2의 코사인 유사도는 0.72~0.98의 좁은 구간에 압축되어 있어 절대 임계값을
쓸 수 없고, 'CIFAR-10', 'LoRA' 같은 고유명사 정확 매칭에도 약하다.
full-text 검색이 후자를 보완하고, RRF가 순위 기반이라 스케일 문제를 우회한다.

RRF(Reciprocal Rank Fusion): score = Σ 1/(k + rank), k=60이 표준 기본값.
점수의 절대 스케일을 무시하고 순위만 쓰기 때문에 이 상황에 정확히 들어맞는다.

**RRF 위에 가중합 재정렬을 얹는다.** RRF는 유사도만 본다 — 최신성과 인용수는
반영하지 않는다. 요구사항은 "최신 논문 우선, 그것도 유사도보다 우선"이므로
세 요소를 지분으로 나눠 갖는 가중합으로 다시 정렬한다:

    최종점수 = 0.35·유사도 + 0.45·최신성 + 0.20·인용도

가중치의 합이 1.0이라 각 숫자가 곧 우선순위다 — 0.45 > 0.35가 "최신성이 유사도보다
중요하다"를 그대로 표현한다. RRF 점수에 배율을 곱하는 방식도 검토했으나, 후보 풀
안에서 RRF가 만드는 격차가 3.61배라 최신성이 그걸 넘으려면 배율 계수가 8까지
올라가야 했다(설명 불가능한 숫자). 검토한 3개 안의 비교는 docs/랭킹_가중치_설계.md.

재정렬은 RRF를 대체하지 않고 그 위에 얹는다. RRF의 강점(스케일이 다른 검색기를
순위만으로 결합)은 그대로 두고, 메타데이터 기반 재정렬을 별도 층으로 분리하는 편이
각 층의 책임이 분명하다.
"""
import logging
from dataclasses import dataclass

from paper_assistant.db.connection import cursor

log = logging.getLogger(__name__)

RRF_K = 60

# LLM에게 넘길 후보 수. **여기서 최종 편수를 정하지 않는다** — 최종 5편은 LLM이 고른다.
#
# 50인 이유는 재현율이 아니라 **판정 대역의 폭**이다. 후보 50편은 전부 코사인 0.93+
# 대역이고(docs/추천_파이프라인_재설계.md §4.3) 임베딩은 그 안에서 순위를 매기지 못한다
# — 상위 20편의 코사인 폭이 0.013뿐이다(설계서 §20). 그 구간을 실제로 가를 수 있는 건
# 본문·참고문헌을 읽는 LLM뿐이므로, 고를 거리를 더 주는 것이 이 구조에서 유일하게
# 의미 있는 레버다. 비용은 후보 1편당 약 400토큰(≈$0.0008)이라 30 → 50이 총비용 +8%다.
#
# 100으로 올리지 않은 이유: 후보 100편을 한 프롬프트에 넣는 리스트와이즈 재정렬은
# 위치 편향으로 순위 품질이 흔들리기 시작하는 구간이라 측정 없이 들어가지 않는다.
RERANK_CANDIDATES = 50

# 재정렬 도입 때 150까지 넓혔다가 50으로 되돌렸고, 2단계 구조 도입과 함께 **100으로
# 올렸다.** 되돌렸던 근거(docs/랭킹_가중치_설계.md §11.6)는 이랬다:
#
#   풀 150 : 0.488 → 0.503  (+3.1%)     ← 표본 150편 기준 노이즈 범위
#   풀  50 : 0.490 → 0.489  (변화 없음)
#
# 그때는 "검색 비용 3배를 지불할 근거로 약하다"가 결론이었다. **지금 다시 올리는
# 이유는 정확도가 아니라 산술이다** — 재정렬은 후보 풀 위에서만 일어나므로, 풀 50
# (합집합 최대 100편)에서 상위 50편을 꺼내면 후보 절반이 사실상 풀 하위가 된다.
# RERANK_CANDIDATES 의 최소 2배는 있어야 상위 절반만 넘길 수 있다.
#
# ⚠️ **풀을 키우면 최신성 요구가 깨지는 지점에 가까워진다.** RRF 최하위 후보의 점수가
# 1/(RRF_K + 풀)이라, 풀이 넓을수록 유사도가 만드는 점수 폭이 커져 최신성이 이기기
# 어려워진다. test_newest_paper_wins_over_the_most_similar_one 의 여유:
#
#   풀  50 → +0.0553      풀 125 → +0.0160
#   풀 100 → +0.0250  ←   풀 150 → +0.0091
#   풀  75 → +0.0373      풀 200 → -0.0007  (깨진다)
#
# 100은 상한(150~200 사이)에서 충분히 떨어져 있다. 이 값이나 W_*, RECENCY_HALF_LIFE를
# 건드리면 반드시 그 테스트를 다시 돌릴 것 (테스트가 상수를 읽으므로 자동으로 따라온다).
CANDIDATE_POOL = 100   # 각 검색기에서 가져올 후보 수

# HNSW는 ef_search(기본 40)보다 많은 행을 반환하지 못한다. CANDIDATE_POOL이 50이라
# 기본값 그대로면 벡터 후보가 40개로 잘려 RRF 결합이 한쪽만 얕아진다 (§20 실측).
#
# 이 값은 **기본 풀에 대한 참고값**이다. 실제 ef_search는 _vector_search 가 그때의
# limit 으로 다시 계산한다 — 여기 상수를 쓰면 hybrid_search(pool=...) 로 풀을 키웠을
# 때 ef_search 가 따라오지 않아 조용히 후보가 잘린다.
HNSW_EF_SEARCH = max(CANDIDATE_POOL * 4, 100)


def _ef_search_for(limit: int) -> int:
    """주어진 후보 수를 채우는 데 필요한 ef_search. limit 과 항상 함께 움직인다."""
    return max(limit * 4, 100)


# ------------------------------------------------------------ 랭킹 가중치
# 합이 1.0이라 각 값이 곧 지분 비율이다. 최신성 > 유사도가 요구사항의 핵심.
#
# **이 셋은 기본 프리셋(balanced)의 값이다.** 사용자가 온보딩에서 "최신 트렌드"나
# "검증된(인용 많은) 논문"을 고르면 RANKING_PRESETS의 다른 조합이 쓰인다. 여기를
# 상수로 남겨 둔 이유는 balanced가 여전히 **기준선**이기 때문이다 — 온보딩을
# 건너뛴 사용자, 옛 분석, 그리고 튜닝 실험이 전부 이 값을 본다.
W_SIMILARITY = 0.35
W_RECENCY = 0.45
W_CITATION = 0.20

# 최신성 감쇠의 반감기(년). 가중치가 "얼마나 중요한가"라면 반감기는 "얼마나 빨리
# 늙는가"다. 실측으로 정했다 — 2025년 논문을 쿼리로(실사용 조건) 반감기만 바꿔가며
# 비교한 결과가 docs/랭킹_가중치_설계.md §11에 있다. 정확도는 어느 반감기든 노이즈
# 범위라 구분되지 않았고, 3년을 고른 이유는 결과가 한 해에 100% 가깝게 쏠리는 것을
# 피하기 위해서다 — 20편이 전부 같은 심사 사이클이면 그 사이클의 우연(특정 AC 성향,
# 그해 유행 주제)이 '패턴'으로 보일 수 있다.
#
# ⚠️ **반감기 상한은 CANDIDATE_POOL에 따라 달라진다.** 요구사항이 깨지는 지점이
# 풀에 묶여 있기 때문이다 — RRF 최하위 후보의 점수가 1/(RRF_K + 풀)이라, 풀이
# 넓을수록 유사도가 만드는 점수 폭이 커져서 최신성이 이기기 어려워진다.
#
#   풀  50 → 유사도 격차 3.61배, 반감기 4년까지 통과 (3년일 때 여유 0.055)
#   풀 150 → 유사도 격차 6.89배, 반감기 3년까지 통과 (3년일 때 여유 0.009)
#
# 따라서 이 값이나 W_* 또는 **CANDIDATE_POOL** 을 건드릴 때는 반드시 아래 테스트를
# 다시 돌릴 것 (테스트가 풀을 읽으므로 자동으로 따라온다):
#   tests/paper_assistant/test_retrieval.py::test_newest_paper_wins_over_the_most_similar_one
RECENCY_HALF_LIFE = 3.0

# citation_percentile이 NULL일 때 쓸 중립값. NULL은 "인용 0회"가 아니라 "S2 보강이
# 닿지 않았다"는 뜻이라(코퍼스의 30.5%) 상도 벌도 주지 않는다. 0으로 채우면 결측률이
# 가장 높은 축인 2025년(58.0%)이 하필 손해를 봐서 최신성 요구와 어긋난다.
NEUTRAL_CITATION_PERCENTILE = 0.5


# --------------------------------------------------- 사용자 선호별 랭킹 프리셋

@dataclass(frozen=True)
class RankingWeights:
    """재정렬 한 벌 — 가중치 3개 **와 반감기**.

    반감기를 같이 묶는 이유는, 이 넷이 따로 움직일 수 있는 값이 아니기 때문이다.
    "인용 많은 논문을 우선하라"를 가중치만으로 구현하면 거의 아무 일도 일어나지
    않는다: citation_percentile은 **같은 연도 안**의 백분위인데, 기본 설정에서는
    결과의 88%가 2025년이고 2025년은 인용 결측률이 58.0%라 그 절반이 중립값
    0.5로 서로 같기 때문이다. 인용도가 실제로 순위를 가르려면 **연도가 퍼져야**
    하고, 연도를 퍼뜨리는 손잡이는 가중치가 아니라 반감기다.
    """
    similarity: float
    recency: float
    citation: float
    half_life: float


def balanced_weights() -> RankingWeights:
    """기본 프리셋. **모듈 상수를 호출 시점에 읽는다.**

    상수를 dict에 미리 박아 두면 recency_score가 피한 것과 똑같은 함정에 빠진다 —
    모듈이 로드될 때 값이 한 번 복사되어, 튜닝 실험이 W_*나 RECENCY_HALF_LIFE를
    바꿔도 조용히 옛 값으로 계산된다.
    """
    return RankingWeights(W_SIMILARITY, W_RECENCY, W_CITATION, RECENCY_HALF_LIFE)


# 온보딩 "최신 논문과 인용이 많은 논문, 뭘 우선할까요?"의 답 → 랭킹 한 벌.
#
# ⚠️ **어떤 프리셋도 "최신성 > 유사도"를 깨서는 안 된다.** 그건 사용자 선호보다
# 상위에 있는 요구사항이고(docs/랭킹_가중치_설계.md §1, 교수님 피드백), 그래서
# cited 프리셋조차 recency > similarity를 유지한다. 인용도에 줄 0.05는 최신성이
# 아니라 **유사도에서** 가져왔다 — 최신성에서 빼면 요구사항이 즉시 깨진다
# (0.35/0.40/0.25 · 4년의 여유는 -0.052다).
#
# 근거는 docs/랭킹_가중치_설계.md §11.7의 조합 탐색이다. 거기서 확인된 것은
# "F1로는 어떤 조합도 서로 구분되지 않는다(전부 0.470~0.485, 표준오차 0.015)"와
# "조합이 실제로 바꾸는 것은 정확도가 아니라 **연도 분포**"였다. 그래서 프리셋의
# 약속도 정확도가 아니라 분포다:
#
#   balanced 0.35/0.45/0.20 · 3년 → 2025년 88.0%  (§11.7 ★ 실측)
#   recent   0.30/0.50/0.20 · 2년 → 2025년 93%+   (양 축 모두 최신 쪽. 실측된 두
#            점(0.35/0.45/0.20·2년 F1 0.485, 0.30/0.50/0.20·3년 F1 0.481) 사이라
#            F1은 그 구간으로 본다. 요구사항 여유는 계산으로 확인된다 — 아래 테스트)
#   cited    0.30/0.45/0.25 · 4년 → 2025년 82%대  (가중치 조합은 §11.7 실측
#            F1 0.478, 반감기 4년도 §11.7 실측. 둘을 겹친 것은 이번이 처음이다)
#
# 프리셋을 늘리거나 숫자를 고치면 tests/paper_assistant/test_retrieval.py의
# test_no_preset_breaks_the_recency_requirement 가 자동으로 따라온다 — 그 테스트가
# 이 딕셔너리를 그대로 읽는다.
_TUNED_PRESETS = {
    "recent": RankingWeights(0.30, 0.50, 0.20, 2.0),
    "cited": RankingWeights(0.30, 0.45, 0.25, 4.0),
}

PRESET_NAMES = ("balanced", "recent", "cited")


def ranking_weights(recency_bias: str | None = None) -> RankingWeights:
    """온보딩 답(recency_bias) → 랭킹 한 벌. 모르는 값이면 기본 프리셋.

    문자열을 **키 조회로만** 쓴다 — 이 값의 출처가 무인증 엔드포인트라
    (schemas.SearchPreferences의 ⚠️ 참고) 계산식에 흘려 넣을 수 없다.
    """
    return _TUNED_PRESETS.get(recency_bias) or balanced_weights()


@dataclass
class SearchResult:
    paper_id: int
    openreview_id: str
    title: str
    abstract: str
    venue: str
    year: int
    decision: str
    rrf_score: float
    vector_rank: int | None
    fts_rank: int | None
    cosine: float | None
    """원시 코사인. **사용자에게 절대 노출 금지** — 쿼리 단위 신뢰도 판정에만 쓴다."""
    match_type: str
    """both(의미+용어) / semantic(의미만) / lexical(용어만). 왜 걸렸는지의 $0 근거."""
    meta_review: str | None = None
    """AC 총평. 개별 리뷰보다 신호가 강해 종합 단계에서 근거로 인용한다."""
    final_score: float = 0.0
    """가중합 재정렬 점수. **결과 정렬은 이 값 기준이다** (rrf_score가 아니라)."""
    recency: float = 0.0
    """최신성 [0,1]. 반감기 3년 기준 2025년=1.00, 2020년=0.32.

    match_type과 같은 결의 $0 근거다 — 왜 이 순위인지 눈으로 확인할 수 있다."""
    citation_percentile: float = NEUTRAL_CITATION_PERCENTILE
    """같은 연도 내 인용 백분위 [0,1]. 결측이면 중립값 0.5."""


# 리뷰가 없는 논문을 후보에서 제외하는 조건. **두 검색기가 반드시 같은 조건을 써야
# 한다** — 한쪽만 걸면 RRF 결합에서 한쪽 순위만 밀려 점수가 조용히 왜곡된다.
#
# 이 서비스가 보여주는 것은 유사 논문 자체가 아니라 그 논문이 받은 리뷰다(README).
# 리뷰가 없는 논문은 후보에 올라봐야 사용자에게 보여줄 것이 없다.
#
# 실측(2026-08-06): 43,515편 중 43,034편(98.9%)이 통과한다. 걸러지는 481편은 전부
# ICLR 2024(329)·2025(152)이고 대부분 withdrawn·desk-reject다. 그리고 리뷰가 있는
# 논문은 **전부** weaknesses 원문이 비어 있지 않으므로, 이 조건 하나로 "리뷰를
# 보여준다"는 약속이 항상 지켜진다.
#
# 1.1%만 걸러지므로 HNSW post-filter로 인한 후보 손실은 무시할 수준이다 —
# ef_search가 limit의 4배라 걸러낸 뒤에도 limit을 채우고 남는다.
_HAS_REVIEWS = "EXISTS (SELECT 1 FROM reviews r WHERE r.paper_id = p.id)"


def _vector_search(cur, embedding, limit: int) -> list[tuple[int, float]]:
    """(paper_id, 코사인 유사도) 순위순. 벡터는 L2 정규화 상태로 저장돼 있다."""
    # ef_search < limit이면 요청한 개수를 못 채운다 (기본 40 < pool 50).
    # **모듈 상수가 아니라 이번 limit 으로 계산한다** — 상수를 쓰면 호출자가
    # pool 을 키웠을 때 ef_search 가 그대로 남아 후보가 조용히 잘린다.
    # SET은 바인드 파라미터를 못 받으므로 set_config를 쓴다. 세 번째 인자 true =
    # 트랜잭션 로컬이라 풀에 반납된 커넥션에 설정이 남지 않는다.
    cur.execute("SELECT set_config('hnsw.ef_search', %s, true)",
                (str(_ef_search_for(limit)),))
    cur.execute(
        f"""
        SELECT p.id, 1 - (p.embedding <=> %s) AS cosine
        FROM papers p
        WHERE p.embedding IS NOT NULL AND {_HAS_REVIEWS}
        ORDER BY p.embedding <=> %s
        LIMIT %s
        """,
        (embedding, embedding, limit),
    )
    return [(row[0], float(row[1])) for row in cur.fetchall()]


def _fulltext_search(cur, query_text: str, limit: int) -> list[int]:
    """ts_rank 상위 paper_id. 고유명사·기법명 정확 매칭을 보완한다.

    주의: plainto_tsquery는 모든 단어를 AND로 결합하므로 초록 전체(수백 단어)를
    넣으면 그 단어를 전부 가진 문서만 걸린다 — 실측 결과 200편 중 1편만 매칭되어
    FTS가 사실상 무력화됐다. 그래서 lexeme을 OR로 결합해 ts_rank가 겹치는 정도로
    순위를 매기게 한다 (BM25에 가까운 동작). 실측: 같은 조건에서 200/200편 매칭.

    ⚠️ lexeme은 반드시 quote_literal로 감싼다. to_tsvector는 URL 같은 토큰을
    그대로 lexeme으로 뱉는데(`github.com/a/b](https://…).`), 여기에 괄호·콜론이
    들어 있어 to_tsquery 파서가 SyntaxError를 낸다. 즉 **초록에 URL이 있으면
    분석이 통째로 실패했다** (held-out 평가에서 발견). 인용은 동작을 바꾸지
    않는다 — 실측으로 매칭 수 33,638편, top-50, 1위까지 인용 전후가 동일하다.
    """
    cur.execute(
        f"""
        WITH q AS (
            SELECT to_tsquery('english', string_agg(quote_literal(lexeme), ' | '))
                   AS query
            FROM unnest(to_tsvector('english', %s))
        )
        SELECT p.id
        FROM papers p, q
        WHERE q.query IS NOT NULL AND p.tsv @@ q.query AND {_HAS_REVIEWS}
        ORDER BY ts_rank(p.tsv, q.query) DESC
        LIMIT %s
        """,
        (query_text, limit),
    )
    return [row[0] for row in cur.fetchall()]


def rrf_fuse(*rank_maps: dict[int, int], k: int = RRF_K) -> dict[int, float]:
    """여러 검색기의 순위를 RRF로 결합. score = Σ 1/(k + rank).

    점수의 절대 스케일을 쓰지 않고 순위만 쓰기 때문에, 스케일이 다른
    검색기(코사인 0.72~0.98 vs ts_rank 0~1)를 그대로 합칠 수 있다.
    """
    scores: dict[int, float] = {}
    for ranks in rank_maps:
        for doc_id, rank in ranks.items():
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


# ------------------------------------------------------------ 가중합 재정렬

def max_rrf(n_retrievers: int = 2, k: int = RRF_K) -> float:
    """RRF가 낼 수 있는 이론 최대값 — 모든 검색기에서 1위일 때.

    유사도를 [0,1]로 정규화하는 분모로 쓴다. 후보 풀 min-max 정규화가 아니라
    이론 최대값을 쓰는 이유는 **쿼리마다 기준이 흔들리지 않게** 하기 위해서다.
    min-max를 쓰면 후보들이 다 비슷하게 좋은 쿼리에서 미세한 차이가 과장 증폭된다.
    """
    return n_retrievers / (k + 1)


def recency_score(year: int, max_year: int,
                  half_life: float | None = None) -> float:
    """최신성 [0,1]. 반감기 half_life년의 지수 감쇠.

    max_year를 하드코딩하지 않고 코퍼스에서 받는 이유는 2026년 논문이 적재돼도
    자동으로 적응하기 위해서다. 코퍼스보다 미래인 연도는 1.0으로 묶는다.

    half_life 기본값을 `= RECENCY_HALF_LIFE`로 쓰지 않는 이유: 파이썬 기본 인자는
    **함수 정의 시점에 한 번** 평가되어 그 값이 박힌다. 그러면 튜닝 실험이
    모듈 상수를 바꿔도 반영되지 않아 조용히 옛 값으로 계산된다. None을 받아
    호출 시점에 상수를 읽으면 상수가 단일 소스로 유지된다.
    """
    if half_life is None:
        half_life = RECENCY_HALF_LIFE
    age = max(max_year - year, 0)
    return 0.5 ** (age / half_life)


def weighted_score(similarity: float, recency: float, citation: float,
                   weights: RankingWeights | None = None) -> float:
    """세 요소의 가중합. 각 인자는 [0,1]로 정규화된 상태여야 한다.

    weights=None이면 기본 프리셋 — 그것도 **호출 시점에** 읽는다
    (balanced_weights 주석 참고).
    """
    w = weights or balanced_weights()
    return (w.similarity * similarity
            + w.recency * recency
            + w.citation * citation)


@dataclass
class RankingSignals:
    """재정렬 결과 한 편분. 최종 점수와 그 근거가 된 성분을 함께 들고 다닌다."""
    final: float
    similarity: float
    recency: float
    citation: float


def rerank(rrf_scores: dict[int, float],
           ranking_fields: dict[int, tuple[int, float | None]],
           max_year: int,
           n_retrievers: int = 2,
           weights: RankingWeights | None = None) -> dict[int, RankingSignals]:
    """RRF 점수 + 메타데이터를 가중합으로 재정렬한다.

    rrf_scores    : {paper_id: RRF 점수}
    ranking_fields: {paper_id: (year, citation_percentile 또는 None)}
    max_year      : 코퍼스의 최신 연도 (최신성 계산 기준점)
    weights       : 사용자 선호 프리셋. None이면 기본(balanced).

    ranking_fields에 없는 논문은 중립값으로 처리한다 — 조회 사이에 사라진 경우라
    어차피 메타데이터 조회에서 탈락한다. 여기서 예외를 내면 검색 전체가 죽는다.
    """
    w = weights or balanced_weights()
    denom = max_rrf(n_retrievers)
    signals: dict[int, RankingSignals] = {}
    for pid, rrf in rrf_scores.items():
        year, percentile = ranking_fields.get(pid, (max_year, None))
        similarity = min(rrf / denom, 1.0)
        recency = recency_score(year, max_year, w.half_life)
        citation = (NEUTRAL_CITATION_PERCENTILE if percentile is None
                    else float(percentile))
        signals[pid] = RankingSignals(
            final=weighted_score(similarity, recency, citation, w),
            similarity=similarity, recency=recency, citation=citation)
    return signals


def _fetch_ranking_fields(cur, paper_ids: list[int]) -> tuple[
        dict[int, tuple[int, float | None]], dict[int, tuple], int]:
    """재정렬에 필요한 최소 컬럼 + 중복 판별 키 + 코퍼스 최신 연도.

    전체 메타데이터(초록·AC 총평)를 후보 전부에 대해 가져오면 낭비라, 재정렬용
    컬럼만 먼저 읽고 상위 top_k가 정해진 뒤에 나머지를 가져온다.

    max(year)는 스칼라 서브쿼리라 한 번만 평가된다. 43,515행 집계가 매 쿼리
    붙지만 HNSW 검색·임베딩에 비하면 무시할 수준이라 캐시하지 않았다 — 캐시하면
    코퍼스에 새 연도가 들어와도 프로세스가 살아 있는 동안 반영되지 않는다.

    반환값 둘째는 `{paper_id: (정규화 제목, venue, 리뷰 수)}` — 중복 제거용이다
    (_dedupe_key 참고). 리뷰 수는 쌍 중 어느 쪽을 남길지 정하는 데 쓴다.
    """
    if not paper_ids:
        return {}, {}, 0
    cur.execute(
        """
        SELECT p.id, p.year, p.citation_percentile,
               lower(btrim(p.title)), p.venue,
               (SELECT count(*) FROM reviews r WHERE r.paper_id = p.id),
               m.max_year
        FROM papers p, (SELECT max(year) AS max_year FROM papers) m
        WHERE p.id = ANY(%s)
        """,
        (paper_ids,),
    )
    rows = cur.fetchall()
    if not rows:
        return {}, {}, 0
    fields = {r[0]: (r[1], r[2]) for r in rows}
    dedupe = {r[0]: (r[3], r[4], r[5]) for r in rows}
    return fields, dedupe, rows[0][6]


def _pick_without_duplicates(ranked, dedupe: dict[int, tuple], top_k: int):
    """같은 논문이 후보에 두 번 들어가지 않게 하면서 상위 top_k를 고른다.

    **코퍼스에 같은 논문이 두 행으로 들어 있다** — NeurIPS 2021에서 301쌍(그 venue의
    21.7%)이다. openreview_id도 forum_id도 다른 별개의 노트라 `paper_id` 기준
    중복 제거로는 걸리지 않는다. 실측: 표본 8쌍 중 6쌍이 후보 50편에 나란히 들어왔다.

    막지 않으면 두 가지가 깨진다. 후보 한 자리가 낭비되고, LLM이 둘 다 고르면
    **사용자에게 같은 논문이 두 번 뜬다** (paper_id가 달라 선정 단계의 중복 제거도
    통과한다).

    ⚠️ **DB에서 지우지 않는 이유**: 두 행의 리뷰가 **하나도 겹치지 않는다**(301쌍 전부,
    쌍당 평균 7.8건). 한쪽을 지우면 실제 리뷰 약 1,170건이 사라진다. 어느 쪽이
    정본인지도 판별되지 않는다 — decision은 301쌍 전부 일치하고, 리뷰 수·평균 평점은
    양방향으로 고르게 갈린다. 그래서 저장은 그대로 두고 **검색에서만** 하나로 접는다.

    남길 쪽은 **리뷰가 많은 행**이다. 어차피 한 쪽만 보여준다면 읽을 것이 많은 편이
    낫다. 같으면 순위가 높은 쪽이 남는다.

    venue를 키에 넣는 이유: ICLR 2024 → NeurIPS 2024 재투고는 **같은 제목이지만 다른
    심사**다. 그건 접으면 안 된다 (submission_links가 추적하는 관계다).
    """
    best: dict[tuple, tuple[int, object]] = {}   # 키 -> (paper_id, signal)
    order: list[tuple] = []                      # 키의 첫 등장 순서 = 순위
    for pid, signal in ranked:
        key = dedupe.get(pid)
        if key is None:                # 조회 사이에 사라진 논문
            key = ("__missing__", pid, 0)
        lookup = key[:2]
        if lookup not in best:
            best[lookup] = (pid, signal)
            order.append(lookup)
            if len(order) == top_k:
                break
        elif key[2] > dedupe.get(best[lookup][0], (None, None, -1))[2]:
            best[lookup] = (pid, signal)   # 리뷰가 더 많은 쪽으로 교체
    return [best[k] for k in order]


def _fetch_metadata(cur, paper_ids: list[int]) -> dict[int, tuple]:
    if not paper_ids:
        return {}
    # meta_review(AC 총평)는 종합 단계에서 근거로 인용한다. 전문은 길어서
    # 여기서 잘라 온다 — 어차피 프롬프트에 넣을 땐 더 줄인다(graph/evidence.py).
    cur.execute(
        """
        SELECT id, openreview_id, title, abstract, venue, year, decision,
               left(meta_review, 2000)
        FROM papers WHERE id = ANY(%s)
        """,
        (paper_ids,),
    )
    return {row[0]: row[1:] for row in cur.fetchall()}


def hybrid_search(embedding, query_text: str, top_k: int | None = None,
                  pool: int | None = None,
                  weights: RankingWeights | None = None) -> list[SearchResult]:
    """벡터·full-text를 RRF로 결합하고 가중합으로 재정렬해 상위 top_k편 반환.

    embedding: SPECTER2로 인코딩한 쿼리 벡터 (L2 정규화된 numpy/list)
    query_text: full-text 검색에 쓸 원문 (보통 제목 + 초록)
    top_k: 반환할 후보 수. None이면 RERANK_CANDIDATES (LLM 재정렬에 넘길 수).
    pool: 각 검색기에서 가져올 후보 수. None이면 CANDIDATE_POOL.
    weights: 사용자 선호 랭킹 프리셋. None이면 기본(balanced) — 즉 온보딩을
        건너뛴 사용자와 옛 호출자는 이 인자가 생기기 전과 정확히 같게 돈다.
        **검색 자체(두 검색기·RRF)는 프리셋과 무관하다** — 프리셋은 이미 뽑힌
        후보를 어떤 순서로 넘길지만 바꾼다.

    **리뷰가 있는 논문만 반환한다** (_HAS_REVIEWS 참고).

    반환 순서는 **final_score 기준**이다. rrf_score도 같이 실어 보내지만 그건
    유사도 성분일 뿐이라 정렬 기준이 아니다. 그리고 이 순서는 최종 순서가 아니다 —
    LLM 재정렬이 이 목록을 받아 다시 고른다.

    top_k·pool 기본값을 `= RERANK_CANDIDATES` / `= CANDIDATE_POOL`로 쓰지 않는 이유는
    recency_score와 같다 — 파이썬 기본 인자는 정의 시점에 한 번 평가되므로, 튜닝
    실험이 모듈 상수를 바꿔도 반영되지 않아 조용히 옛 값으로 검색하게 된다.
    """
    if top_k is None:
        top_k = RERANK_CANDIDATES
    if pool is None:
        pool = CANDIDATE_POOL
    with cursor() as cur:
        vector_hits = _vector_search(cur, embedding, pool)
        fts_hits = _fulltext_search(cur, query_text, pool)

        vector_ranks = {pid: i + 1 for i, (pid, _) in enumerate(vector_hits)}
        cosines = dict(vector_hits)
        fts_ranks = {pid: i + 1 for i, pid in enumerate(fts_hits)}

        scores = rrf_fuse(vector_ranks, fts_ranks)

        # 재정렬은 후보 **전체**를 대상으로 해야 의미가 있다. top_k로 자른 뒤에
        # 재정렬하면 최신 논문이 잘려나간 뒤라 끌어올릴 대상 자체가 없다.
        ranking_fields, dedupe, max_year = _fetch_ranking_fields(cur, list(scores))
        signals = rerank(scores, ranking_fields, max_year, weights=weights)

        # 자르기 **전에** 중복을 접는다. 자른 뒤에 접으면 사라진 자리만큼 후보가
        # 줄어들어, 요청한 50편이 아니라 48편이 조용히 나간다.
        ranked = _pick_without_duplicates(
            sorted(signals.items(), key=lambda kv: kv[1].final, reverse=True),
            dedupe, top_k)
        meta = _fetch_metadata(cur, [pid for pid, _ in ranked])

    results = []
    for pid, signal in ranked:
        if pid not in meta:
            continue
        (openreview_id, title, abstract, venue, year, decision,
         meta_review) = meta[pid]
        results.append(SearchResult(
            paper_id=pid,
            openreview_id=openreview_id,
            title=title,
            abstract=abstract,
            venue=venue,
            year=year,
            decision=decision,
            rrf_score=scores[pid],
            vector_rank=vector_ranks.get(pid),
            fts_rank=fts_ranks.get(pid),
            cosine=cosines.get(pid),
            match_type=match_type(vector_ranks.get(pid), fts_ranks.get(pid)),
            meta_review=meta_review,
            final_score=signal.final,
            recency=signal.recency,
            citation_percentile=signal.citation,
        ))
    return results


def match_type(vector_rank: int | None, fts_rank: int | None) -> str:
    """왜 이 논문이 걸렸는지. 검색기 두 개의 히트 여부만 보면 된다 — 비용 $0.

    both     : 임베딩과 용어 양쪽 모두 → 가장 믿을 만한 매칭
    semantic : 임베딩만 → 접근은 비슷한데 쓰는 용어가 다르다
    lexical  : 용어만 → 같은 단어를 쓰지만 접근은 다를 수 있다
    """
    if vector_rank is not None and fts_rank is not None:
        return "both"
    return "semantic" if vector_rank is not None else "lexical"
