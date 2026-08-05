"""하이브리드 검색: SPECTER2 벡터 + Postgres full-text, RRF로 결합 후 재정렬.

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

# 재정렬은 이미 검색된 것만 재배치할 수 있다. 풀이 좁으면 최신성 가중치가 끌어올릴
# 후보 자체가 없어서 효과가 안 난다. 그래서 재정렬 도입과 함께 50 → 150으로 넓혔다.
CANDIDATE_POOL = 150   # 각 검색기에서 가져올 후보 수

# HNSW는 ef_search(기본 40)보다 많은 행을 반환하지 못한다. CANDIDATE_POOL이 50이라
# 기본값 그대로면 벡터 후보가 40개로 잘려 RRF 결합이 한쪽만 얕아진다 (§20 실측).
HNSW_EF_SEARCH = max(CANDIDATE_POOL * 4, 100)

# ------------------------------------------------------------ 랭킹 가중치
# 합이 1.0이라 각 값이 곧 지분 비율이다. 최신성 > 유사도가 요구사항의 핵심.
W_SIMILARITY = 0.35
W_RECENCY = 0.45
W_CITATION = 0.20

# 최신성 감쇠의 반감기(년). 가중치가 "얼마나 중요한가"라면 반감기는 "얼마나 빨리
# 늙는가"다. 실측으로 정한 값이다 — 2025년 논문 150편을 쿼리로 넣어(실사용 조건)
# 반감기만 바꿔가며 비교한 결과가 docs/랭킹_가중치_설계.md §11.4에 있다:
#
#   반감기 2년 → 결과의 94.8%가 2025년, F1@3 0.502
#   반감기 3년 → 89.3%,                  F1@3 0.503   ← 채택
#   반감기 4년 → 83.6%,                  F1@3 0.500   (아래 이유로 탈락)
#
# 정확도는 셋 다 노이즈 범위라 구분되지 않는다. 3년을 고른 이유는 결과가 한 해에
# 100% 가깝게 쏠리는 것을 피하기 위해서다 — 20편이 전부 같은 심사 사이클이면 그
# 사이클의 우연(특정 AC 성향, 그해 유행 주제)이 '패턴'으로 보일 수 있다.
#
# ⚠️ **4년 이상으로 올리지 말 것.** 4년부터는 유사도가 최신성을 이겨서 요구사항이
# 깨진다 (유사도 만점인 2020년 논문이 유사도 꼴찌인 2025년 논문을 누른다).
# 가중치 0.45 > 0.35는 그대로인데도 그렇다. 3년은 통과하되 여유가 0.009로 좁으니,
# 이 값이나 W_* 를 건드릴 때는 반드시 아래 테스트를 다시 돌릴 것:
#   tests/paper_assistant/test_retrieval.py::test_newest_paper_wins_over_the_most_similar_one
RECENCY_HALF_LIFE = 3.0

# citation_percentile이 NULL일 때 쓸 중립값. NULL은 "인용 0회"가 아니라 "S2 보강이
# 닿지 않았다"는 뜻이라(코퍼스의 30.5%) 상도 벌도 주지 않는다. 0으로 채우면 결측률이
# 가장 높은 축인 2025년(58.0%)이 하필 손해를 봐서 최신성 요구와 어긋난다.
NEUTRAL_CITATION_PERCENTILE = 0.5


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
    """최신성 [0,1]. 2025년=1.00, 2020년=0.18. match_type과 같은 결의 $0 근거다."""
    citation_percentile: float = NEUTRAL_CITATION_PERCENTILE
    """같은 연도 내 인용 백분위 [0,1]. 결측이면 중립값 0.5."""


def _vector_search(cur, embedding, limit: int) -> list[tuple[int, float]]:
    """(paper_id, 코사인 유사도) 순위순. 벡터는 L2 정규화 상태로 저장돼 있다."""
    # ef_search < limit이면 요청한 개수를 못 채운다 (기본 40 < pool 50).
    # SET은 바인드 파라미터를 못 받으므로 set_config를 쓴다. 세 번째 인자 true =
    # 트랜잭션 로컬이라 풀에 반납된 커넥션에 설정이 남지 않는다.
    cur.execute("SELECT set_config('hnsw.ef_search', %s, true)",
                (str(HNSW_EF_SEARCH),))
    cur.execute(
        """
        SELECT id, 1 - (embedding <=> %s) AS cosine
        FROM papers
        WHERE embedding IS NOT NULL
        ORDER BY embedding <=> %s
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
        """
        WITH q AS (
            SELECT to_tsquery('english', string_agg(quote_literal(lexeme), ' | '))
                   AS query
            FROM unnest(to_tsvector('english', %s))
        )
        SELECT p.id
        FROM papers p, q
        WHERE q.query IS NOT NULL AND p.tsv @@ q.query
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


def weighted_score(similarity: float, recency: float, citation: float) -> float:
    """세 요소의 가중합. 각 인자는 [0,1]로 정규화된 상태여야 한다."""
    return (W_SIMILARITY * similarity
            + W_RECENCY * recency
            + W_CITATION * citation)


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
           n_retrievers: int = 2) -> dict[int, RankingSignals]:
    """RRF 점수 + 메타데이터를 가중합으로 재정렬한다.

    rrf_scores    : {paper_id: RRF 점수}
    ranking_fields: {paper_id: (year, citation_percentile 또는 None)}
    max_year      : 코퍼스의 최신 연도 (최신성 계산 기준점)

    ranking_fields에 없는 논문은 중립값으로 처리한다 — 조회 사이에 사라진 경우라
    어차피 메타데이터 조회에서 탈락한다. 여기서 예외를 내면 검색 전체가 죽는다.
    """
    denom = max_rrf(n_retrievers)
    signals: dict[int, RankingSignals] = {}
    for pid, rrf in rrf_scores.items():
        year, percentile = ranking_fields.get(pid, (max_year, None))
        similarity = min(rrf / denom, 1.0)
        recency = recency_score(year, max_year)
        citation = (NEUTRAL_CITATION_PERCENTILE if percentile is None
                    else float(percentile))
        signals[pid] = RankingSignals(
            final=weighted_score(similarity, recency, citation),
            similarity=similarity, recency=recency, citation=citation)
    return signals


def _fetch_ranking_fields(cur, paper_ids: list[int]
                          ) -> tuple[dict[int, tuple[int, float | None]], int]:
    """재정렬에 필요한 최소 컬럼만 + 코퍼스 최신 연도.

    전체 메타데이터(초록·AC 총평)를 후보 300편분 가져오면 낭비라, 재정렬용
    컬럼만 먼저 읽고 상위 top_k가 정해진 뒤에 나머지를 가져온다.

    max(year)는 스칼라 서브쿼리라 한 번만 평가된다. 43,515행 집계가 매 쿼리
    붙지만 HNSW 검색·임베딩에 비하면 무시할 수준이라 캐시하지 않았다 — 캐시하면
    코퍼스에 새 연도가 들어와도 프로세스가 살아 있는 동안 반영되지 않는다.
    """
    if not paper_ids:
        return {}, 0
    cur.execute(
        """
        SELECT p.id, p.year, p.citation_percentile, m.max_year
        FROM papers p, (SELECT max(year) AS max_year FROM papers) m
        WHERE p.id = ANY(%s)
        """,
        (paper_ids,),
    )
    rows = cur.fetchall()
    if not rows:
        return {}, 0
    fields = {row[0]: (row[1], row[2]) for row in rows}
    return fields, rows[0][3]


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


def hybrid_search(embedding, query_text: str, top_k: int = 20,
                  pool: int = CANDIDATE_POOL) -> list[SearchResult]:
    """벡터·full-text를 RRF로 결합하고 가중합으로 재정렬해 상위 top_k편 반환.

    embedding: SPECTER2로 인코딩한 쿼리 벡터 (L2 정규화된 numpy/list)
    query_text: full-text 검색에 쓸 원문 (보통 제목 + 초록)

    반환 순서는 **final_score 기준**이다. rrf_score도 같이 실어 보내지만 그건
    유사도 성분일 뿐이라 정렬 기준이 아니다.
    """
    with cursor() as cur:
        vector_hits = _vector_search(cur, embedding, pool)
        fts_hits = _fulltext_search(cur, query_text, pool)

        vector_ranks = {pid: i + 1 for i, (pid, _) in enumerate(vector_hits)}
        cosines = dict(vector_hits)
        fts_ranks = {pid: i + 1 for i, pid in enumerate(fts_hits)}

        scores = rrf_fuse(vector_ranks, fts_ranks)

        # 재정렬은 후보 **전체**를 대상으로 해야 의미가 있다. top_k로 자른 뒤에
        # 재정렬하면 최신 논문이 잘려나간 뒤라 끌어올릴 대상 자체가 없다.
        ranking_fields, max_year = _fetch_ranking_fields(cur, list(scores))
        signals = rerank(scores, ranking_fields, max_year)

        ranked = sorted(signals.items(), key=lambda kv: kv[1].final,
                        reverse=True)[:top_k]
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
