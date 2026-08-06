"""LangGraph 노드 7종.

지능이 필요한 노드(재정렬·태깅·종합)만 LLM을 쓰고, 나머지는 코드로 처리한다.
LLM이 None이면(예산 off) 결정론적 스텁을 만들어 DAG를 $0으로 검증할 수 있다.

노드는 embedder / llm을 클로저로 주입받는다 (pipeline.build에서 바인딩).
"""
import json
import logging

from paper_assistant.db.connection import cursor
from paper_assistant.embedding.specter2 import (
    CONFIDENCE_MESSAGES, retrieval_confidence)
from paper_assistant.graph.clustering import aggregate_by_aspect
from paper_assistant.graph.evidence import (
    build_evidence, build_evidence_for_selected, format_for_prompt,
    validate_citations)
from paper_assistant.graph.llm import (
    HAIKU, RERANK_EFFORT, RERANK_MAX_TOKENS, SONNET, SONNET_EFFORT,
    SONNET_MAX_TOKENS)
from paper_assistant.graph.ratings import attach_paper_ratings, build_rating_context
from paper_assistant.graph.state import PipelineState
from paper_assistant.db.stats import (
    conference_of, load_base_rates, load_venue_stats)
from paper_assistant.retrieval.hybrid_search import hybrid_search
from paper_assistant.schemas import (
    PaperSelection, Report, ResubmissionFlow, RetrievalConfidence, ReviewDetail,
    ReviewPattern, SelectedPaper, SimilarityTag, SimilarPaper, VenueTrend)

log = logging.getLogger(__name__)

TAG_KINDS = ("methodology", "dataset", "problem_setting", "citation")


# ---------------------------------------------------------------- input
def input_node(state: PipelineState, embedder, llm) -> dict:
    """PDF면 제목/초록 추출, 텍스트면 통과."""
    if state.get("pdf_bytes") and not state.get("query_abstract"):
        from paper_assistant.pdf.extract import extract_title_abstract
        title, abstract = extract_title_abstract(state["pdf_bytes"], llm=llm)
        return {"query_title": title, "query_abstract": abstract}
    return {}


# ------------------------------------------------------------ retrieval
def retrieval_node(state: PipelineState, embedder, llm) -> dict:
    """SPECTER2 임베딩 → 하이브리드 검색으로 LLM 재정렬에 넘길 후보 확보.

    top_k를 넘기지 않는다 — 후보 수는 hybrid_search.RERANK_CANDIDATES 하나가 소유한다.
    여기서 숫자를 다시 적으면 상수를 바꿔도 반영되지 않는 자리가 하나 더 생긴다.
    """
    title = state["query_title"]
    abstract = state.get("query_abstract", "")
    qvec = embedder.encode_one(title, abstract).numpy()
    results = hybrid_search(qvec, f"{title} {abstract}")

    return {"query_embedding": qvec.tolist(), "similar_papers": results,
            "confidence": confidence_from(results)}


def confidence_from(papers) -> RetrievalConfidence:
    """검색 결과로 '이 결과를 믿어도 되는지'를 판정한다.

    검색 단계에서 계산한다 — 재정렬이 이 값을 보고 LLM을 부를지 정하므로 종합까지
    미룰 수 없다. 순수 함수로 뽑아 둔 이유는 DB·임베더 없이 검증하기 위해서다.

    ⚠️ **코사인은 벡터 검색에 걸린 논문만 가진다** (FTS-only는 None). 그리고 결과
    순서는 RRF+가중합 순서라 벡터 순위와 다르므로, 벡터 순위로 다시 정렬해야 상위
    k개를 제대로 고른다. 결과 순서 그대로 자르면 엉뚱한 코사인으로 판정하게 된다.
    """
    ranked = [p.cosine for p in sorted(
        (p for p in papers if p.cosine is not None and p.vector_rank is not None),
        key=lambda p: p.vector_rank)]
    level, evidence = retrieval_confidence(ranked)
    return RetrievalConfidence(
        level=level, evidence=round(evidence, 4), is_reliable=level != "weak",
        message=CONFIDENCE_MESSAGES[level])


# ------------------------------------------------- LLM 재정렬 (2단계 선정)
RERANK_LIMIT = 5      # 사용자에게 보여줄 최대 편수
CONFIDENCES = ("high", "medium", "low")

# 배열 길이 제약은 구조화 출력이 지원하지 않는다 — "최대 5편"은 코드에서 자른다.
_SELECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "selections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "paper_id": {"type": "integer"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "string", "enum": list(CONFIDENCES)},
                },
                "required": ["paper_id", "reason", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["selections"],
    "additionalProperties": False,
}

_RERANK_SYSTEM = (
    "You are given a researcher's paper as a PDF, and a list of candidate papers "
    "retrieved from a corpus of ICLR/NeurIPS submissions. Pick the candidates that "
    "are genuinely most similar to the PDF, ordered most similar first.\n"
    "\n"
    "The PDF is the full paper — read its method, experiments, and reference list, "
    "not just the abstract. That is why you are being asked instead of the embedding "
    "search that produced these candidates: it only compared titles and abstracts.\n"
    "\n"
    f"Pick AT MOST {RERANK_LIMIT}. **Pick fewer when fewer genuinely match, and pick "
    "none if none do.** The candidates were retrieved by similarity search, so they "
    "all look plausible; that is not evidence that they are similar. Returning three "
    "honest matches is a better answer than five padded ones, because the product "
    "shows the user what reviewers said about these exact papers.\n"
    "\n"
    "What counts as similar: the same research problem, a comparable method or "
    "approach, or the same evaluation setting. Sharing only a dataset or a buzzword "
    "is not enough — a paper that uses the same benchmark for an unrelated task is "
    "not similar. Papers cited by the PDF are strong evidence of genuine relatedness.\n"
    "\n"
    "When two candidates are similar to a comparable degree, prefer the more recent "
    "one: the user's goal is to see what reviewers said, and recent reviews reflect "
    "current expectations.\n"
    "\n"
    "Do NOT let a paper's decision (accept/reject) affect your ranking. Rejected "
    "papers carry the most informative reviews for this product.\n"
    "\n"
    "paper_id MUST be copied exactly from the candidate list. Never invent one — an "
    "id that is not in the list is discarded and the slot is wasted.\n"
    "\n"
    "reason: one sentence, in Korean, naming the concrete thing they share (\"둘 다 "
    "분자 그래프에 메시지 패싱을 쓰고 QM9로 평가한다\"). It is shown to the user as "
    "the justification, so a generic sentence that would fit any pair is useless. "
    "Do not use a numeric similarity score anywhere — we cannot compute one.\n"
    "confidence: high only when the shared substance is unmistakable from the PDF."
)


def llm_rerank_node(state: PipelineState, embedder, llm) -> dict:
    """검색 후보 중에서 **정말로 비슷한** 논문을 LLM이 고른다 (개편의 핵심 단계).

    돌리지 않는 경우가 둘이다:
      - llm이 없다(예산 off) → 검색 상위 5편으로 결정론적 스텁.
      - 검색 신뢰도가 weak → **호출 자체를 건너뛴다.** 도메인 밖 PDF를 넣어도 LLM은
        후보 50편 중 '가장 비슷한 5편'을 성실히 골라내므로, 여기서 막지 않으면 요리
        레시피에도 그럴듯한 답이 나온다. 비용도 잘못된 결과도 아낀다.
      - pdf_bytes가 없다 → 옛 초안(텍스트로 만든 것). 넘길 원문이 없으니 스텁.
    """
    papers = state.get("similar_papers", [])
    if not papers:
        return {"selections": []}

    confidence = state.get("confidence")
    if confidence is not None and not confidence.is_reliable:
        log.info("검색 신뢰도 weak — LLM 재정렬을 건너뜁니다.")
        return {"selections": []}

    pdf_bytes = state.get("pdf_bytes")
    if llm is None or not pdf_bytes:
        return {"selections": _stub_selections(papers)}

    candidates = [{"paper_id": p.paper_id, "title": p.title,
                   "abstract": (p.abstract or "")[:1500],
                   "venue": p.venue, "year": p.year} for p in papers]
    raw = llm.structured_with_pdf(
        SONNET, _RERANK_SYSTEM, pdf_bytes,
        json.dumps({"candidates": candidates}, ensure_ascii=False),
        _SELECTION_SCHEMA, max_tokens=RERANK_MAX_TOKENS,
        output_config={"effort": RERANK_EFFORT})

    return {"selections": _validated_selections(raw.get("selections", []), papers)}


def _stub_selections(papers) -> list[PaperSelection]:
    """LLM 없이 검색 상위 5편. 배선을 $0으로 검증하기 위한 결정론적 출력이다.

    reason에 '검색 순위'라고 분명히 적는다 — 스텁 결과가 LLM 판정으로 오인되면
    개편의 효과를 측정할 수 없게 된다(Report.used_llm과 같은 규약).
    """
    return [PaperSelection(
        paper_id=p.paper_id, rank=i + 1,
        reason="검색 순위 기준 (LLM 재정렬을 돌리지 않았습니다)",
        confidence="low") for i, p in enumerate(papers[:RERANK_LIMIT])]


def _validated_selections(raw: list, papers) -> list[PaperSelection]:
    """모델이 돌려준 선정을 후보 목록과 대조한다.

    ⚠️ **이 검증이 없으면 '인용해 달라'는 부탁일 뿐이다.** 후보에 없는 paper_id가
    화면에 뜨면 존재하지 않는 논문의 리뷰를 조회하려 들고, 조회가 비면 사용자에게는
    그냥 빈 카드로 보인다 — 지어냈다는 사실이 드러나지 않는다.
    (graph/evidence.py의 validate_citations와 같은 규율이다.)

    중복도 버린다. 같은 논문을 두 번 고르면 5칸 중 하나가 낭비되고, 화면에는 같은
    논문이 두 번 뜬다.
    """
    known = {p.paper_id for p in papers}
    out: list[PaperSelection] = []
    seen: set[int] = set()
    for item in raw:
        pid = item.get("paper_id")
        if pid not in known:
            log.warning("후보에 없는 paper_id를 버립니다: %r", pid)
            continue
        if pid in seen:
            log.warning("중복 선정을 버립니다: %s", pid)
            continue
        seen.add(pid)
        out.append(PaperSelection(
            paper_id=pid, rank=len(out) + 1,
            reason=(item.get("reason") or "").strip()[:300],
            confidence=item.get("confidence")
            if item.get("confidence") in CONFIDENCES else "low"))
        if len(out) == RERANK_LIMIT:
            break       # 스키마로 배열 길이를 못 거는 대신 여기서 자른다
    return out


# ------------------------------------------- 선정 논문의 리뷰 조회 (no LLM)
MAX_POINTS_PER_PAPER = 3    # 근거 풀에 넣을 지적 문장 수 (논문당)


def review_fetch_node(state: PipelineState, embedder, llm) -> dict:
    """선정된 5편의 **리뷰 전문**을 가져온다 — 이 서비스가 실제로 파는 것.

    지적항목(review_points)도 함께 읽지만 그건 근거 풀(evidence)을 만들기 위한
    것이고, Report에는 중복해 싣지 않는다. 근거 항목이 이미 그 문장을 들고 있다.
    """
    selections = state.get("selections", [])
    if not selections:
        return {"selected_papers": [], "selection_points": {}}

    ids = [s.paper_id for s in selections]
    with cursor() as cur:
        cur.execute(
            """
            SELECT id, openreview_id, forum_id, title, venue, year, decision,
                   meta_review
            FROM papers WHERE id = ANY(%s)
            """, (ids,))
        meta = {r[0]: r[1:] for r in cur.fetchall()}

        cur.execute(
            """
            SELECT paper_id, rating, rating_raw, confidence, summary, strengths,
                   weaknesses, questions, needs_llm_split
            FROM reviews WHERE paper_id = ANY(%s)
            ORDER BY paper_id, rating DESC NULLS LAST, id
            """, (ids,))
        reviews: dict[int, list[ReviewDetail]] = {}
        for r in cur.fetchall():
            reviews.setdefault(r[0], []).append(ReviewDetail(
                rating=float(r[1]) if r[1] is not None else None,
                rating_raw=r[2],
                confidence=float(r[3]) if r[3] is not None else None,
                summary=r[4], strengths=r[5], weaknesses=r[6], questions=r[7],
                is_unsplit=bool(r[8])))

        # 근거로 인용할 지적 문장. 미분리 리뷰에서 나온 것은 본문 전체를 weakness로
        # 라벨링한 것이라 '지적'이라 단정할 수 없어 뒤로 민다 (from_unsplit 오름차순).
        cur.execute(
            """
            SELECT rp.paper_id, rp.id, rp.aspect, rp.text, r.needs_llm_split
            FROM review_points rp JOIN reviews r ON r.id = rp.review_id
            WHERE rp.paper_id = ANY(%s) AND rp.sentiment = 'weakness'
            ORDER BY rp.paper_id, r.needs_llm_split, (rp.aspect = 'other'), rp.id
            """, (ids,))
        points: dict[int, list[dict]] = {}
        for pid, point_id, aspect, text, unsplit in cur.fetchall():
            bucket = points.setdefault(pid, [])
            if len(bucket) < MAX_POINTS_PER_PAPER:
                bucket.append({"point_id": point_id, "aspect": aspect,
                               "text": text, "from_unsplit": bool(unsplit)})

    selected: list[SelectedPaper] = []
    for s in selections:
        if s.paper_id not in meta:
            # 선정과 조회 사이에 사라진 논문. 여기서 예외를 내면 분석 전체가 죽는다.
            log.warning("선정된 논문을 조회하지 못했습니다: %s", s.paper_id)
            continue
        or_id, forum_id, title, venue, year, decision, meta_review = meta[s.paper_id]
        rs = reviews.get(s.paper_id, [])
        ratings = [r.rating for r in rs if r.rating is not None]
        selected.append(SelectedPaper(
            paper_id=s.paper_id, openreview_id=or_id, title=title, venue=venue,
            year=year, decision=decision,
            # 포럼은 forum_id 기준이지만 미수집 논문이 있어 openreview_id로 폴백한다.
            openreview_url=f"https://openreview.net/forum?id={forum_id or or_id}",
            pdf_url=f"https://openreview.net/pdf?id={or_id}",
            rank=len(selected) + 1, reason=s.reason, confidence=s.confidence,
            meta_review=meta_review, reviews=rs,
            avg_rating=round(sum(ratings) / len(ratings), 2) if ratings else None,
            rating_count=len(rs),
            rating_spread=round(max(ratings) - min(ratings), 2) if ratings else None,
        ))
    return {"selected_papers": selected,
            "selection_points": {p.paper_id: points.get(p.paper_id, [])
                                 for p in selected}}


# ----------------------------------------------- similarity tagging (LLM)
def similarity_tagging_node(state: PipelineState, embedder, llm) -> dict:
    """상위 논문마다 '왜 유사한가' 태깅. LLM off면 스텁."""
    papers = state.get("similar_papers", [])[:10]
    tags: dict[int, list[SimilarityTag]] = {}

    if llm is None:
        # 스텁: 태그 없이 통과 (배선 검증용)
        return {"similarity_tags": {p.paper_id: [] for p in papers}}

    query = f"{state['query_title']}\n{state.get('query_abstract','')}"
    system = (
        "You compare a query paper to a candidate paper and explain why they are "
        "similar. Return JSON: {\"tags\": [{\"kind\": one of "
        "[methodology, dataset, problem_setting, citation], \"reason\": short}]}. "
        "Only include tags that genuinely apply. Reasons under 15 words.")
    for p in papers:
        user = (f"QUERY:\n{query}\n\nCANDIDATE:\n{p.title}\n{p.abstract}")
        out = llm.json(HAIKU, system, user, max_tokens=400)
        parsed = []
        for t in out.get("tags", []):
            if t.get("kind") in TAG_KINDS and t.get("reason"):
                parsed.append(SimilarityTag(kind=t["kind"], reason=t["reason"][:120]))
        tags[p.paper_id] = parsed
    return {"similarity_tags": tags}


# ------------------------------------------------- review analysis (no LLM)
def review_analysis_node(state: PipelineState, embedder, llm) -> dict:
    """유사 논문들의 지적항목을 aspect별로 집계 (쿼리 시점, 임베딩 불필요).

    SPECTER2 임베딩 클러스터링은 리뷰 문장에 부적합해(§14) aspect 기반 집계를 쓴다.
    빈도가 아니라 **코퍼스 base rate 대비 lift**로 정렬하고, 이웃의 당락 정보를
    함께 넘겨 "이 지적을 받고도 붙었는가"를 대조한다 (§18).
    """
    papers = state.get("similar_papers", [])
    paper_ids = [p.paper_id for p in papers]
    if not paper_ids:
        return {"review_patterns": []}

    with cursor() as cur:
        # '측정 가능한' 이웃 = 모든 리뷰가 파싱된 논문. 강/약점 미분리 리뷰 중
        # 약점 섹션을 못 살린 것이 하나라도 있으면 그 논문의 지적을 전부 봤다고
        # 할 수 없어, 분모에 넣으면 '지적 안 받음'으로 잘못 세어진다.
        # build_base_rates.py의 분모 정의와 반드시 같아야 lift가 성립한다.
        cur.execute(
            """
            SELECT r.paper_id
            FROM reviews r
            LEFT JOIN (
                SELECT DISTINCT review_id FROM review_points
                WHERE sentiment = 'weakness'
            ) x ON x.review_id = r.id
            WHERE r.paper_id = ANY(%s)
            GROUP BY r.paper_id
            HAVING bool_and(NOT r.needs_llm_split OR x.review_id IS NOT NULL)
            """,
            (paper_ids,))
        measurable = {r[0] for r in cur.fetchall()}
        if not measurable:
            return {"review_patterns": []}

        # id를 같이 읽는다 — 요약이 인용한 문장을 review_points.id로 역추적한다.
        # needs_llm_split도 읽는다: 미분리 리뷰의 항목은 본문 전체를 weakness로
        # 라벨링한 것이라 요약·칭찬 문장이 섞여 있다. 집계 분모에는 그대로 쓰되
        # (base rate 정의와 맞춰야 한다), 인용할 대표 문장으로는 뒤로 미룬다.
        cur.execute(
            """
            SELECT p.id, p.paper_id, p.aspect, p.text, r.needs_llm_split
            FROM review_points p JOIN reviews r ON r.id = p.review_id
            WHERE p.paper_id = ANY(%s) AND p.sentiment = 'weakness'
            """,
            (sorted(measurable),))
        points = [{"point_id": r[0], "paper_id": r[1], "aspect": r[2],
                   "text": r[3], "from_unsplit": bool(r[4])}
                  for r in cur.fetchall()]

    patterns = aggregate_by_aspect(
        points,
        total_papers=len(measurable),
        base_rates=load_base_rates(),
        decisions={p.paper_id: p.decision for p in papers},
        all_paper_ids=measurable,
    )
    return {"review_patterns": patterns}


# --------------------------------------------------- venue trend (no LLM)
def venue_trend_node(state: PipelineState, embedder, llm) -> dict:
    """유사 논문들의 게재 결과 + 리뷰 점수를 SQL 집계.

    accept율은 코퍼스 기준선과 함께 낸다 — NeurIPS는 코퍼스의 95%가 accept라
    절대값이 실제 채택률처럼 읽히면 안 된다 (§19).
    """
    papers = state.get("similar_papers", [])
    paper_ids = [p.paper_id for p in papers]
    if not paper_ids:
        return {"venue_trends": [], "paper_ratings": {}}

    with cursor() as cur:
        # 학회 단위(ICLR/NeurIPS)로 집계 — 연도별로 쪼개면 셀당 표본이 1~3편이라
        # accept율이 통계적으로 무의미해진다(§14 실측). split_part로 연도를 떼고 합침.
        cur.execute(
            """
            SELECT split_part(venue, ' ', 1) AS conf,
                   count(*) AS n,
                   count(*) FILTER (WHERE decision LIKE 'accept%%') AS accepts
            FROM papers WHERE id = ANY(%s)
            GROUP BY split_part(venue, ' ', 1) ORDER BY n DESC
            """,
            (paper_ids,))
        trends = [VenueTrend(venue=r[0], paper_count=r[1], accept_count=r[2],
                             accept_rate=round(r[2] / r[1], 3) if r[1] else 0.0)
                  for r in cur.fetchall()]

        # 논문별 리뷰 점수 (평균·개수·최고최저차). 168,217건 전부 rating 보유.
        cur.execute(
            """
            SELECT paper_id, avg(rating), count(*), max(rating) - min(rating)
            FROM reviews WHERE paper_id = ANY(%s) AND rating IS NOT NULL
            GROUP BY paper_id
            """,
            (paper_ids,))
        ratings = {r[0]: {"avg": float(r[1]), "count": r[2],
                          "spread": float(r[3]) if r[3] is not None else None}
                   for r in cur.fetchall()}

        # 재투고 흐름: 유사 논문이 한쪽 끝인 링크를 venue쌍으로 집계
        cur.execute(
            """
            SELECT e.venue AS from_v, l.venue AS to_v, count(*) AS n
            FROM submission_links sl
            JOIN papers e ON e.id = sl.earlier_paper_id
            JOIN papers l ON l.id = sl.later_paper_id
            WHERE sl.earlier_paper_id = ANY(%s) OR sl.later_paper_id = ANY(%s)
            GROUP BY e.venue, l.venue ORDER BY n DESC
            """,
            (paper_ids, paper_ids))
        flows = [ResubmissionFlow(from_venue=r[0], to_venue=r[1], count=r[2])
                 for r in cur.fetchall()]

    _attach_venue_baselines(trends)
    return {"venue_trends": trends, "resubmission_flows": flows,
            "paper_ratings": ratings}


def _attach_venue_baselines(trends: list[VenueTrend]) -> None:
    """학회 단위 accept율에 코퍼스 기준선과 편향 플래그를 붙인다.

    trends는 학회 단위(ICLR/NeurIPS)인데 venue_stats는 venue×연도 단위라,
    학회에 속한 연도들을 논문 수 가중으로 합쳐 기준선을 만든다.
    """
    stats = load_venue_stats()
    if not stats:
        return
    for t in trends:
        members = [s for v, s in stats.items() if conference_of(v) == t.venue]
        if not members:
            continue
        total = sum(s.papers for s in members)
        if not total:
            continue
        t.corpus_accept_rate = round(
            sum(s.accept_rate * s.papers for s in members) / total, 3)
        if t.corpus_accept_rate:
            t.accept_lift = round(t.accept_rate / t.corpus_accept_rate, 2)
        t.is_coverage_biased = any(s.is_coverage_biased for s in members)


# ---------------------------------------------------- synthesis (LLM)
def synthesis_node(state: PipelineState, embedder, llm) -> dict:
    """세 분석을 종합해 Report 조립 + 마크다운 요약 생성."""
    papers = state.get("similar_papers", [])
    tags = state.get("similarity_tags", {})
    patterns = state.get("review_patterns", [])
    trends = state.get("venue_trends", [])

    ratings = state.get("paper_ratings", {})
    venue_stats = load_venue_stats()

    similar = []
    for i, p in enumerate(papers):
        sp = SimilarPaper(
            paper_id=p.paper_id, openreview_id=p.openreview_id, title=p.title,
            venue=p.venue, year=p.year, decision=p.decision,
            rank=i + 1, match_type=p.match_type, tags=tags.get(p.paper_id, []))
        attach_paper_ratings(sp, venue_stats.get(p.venue), ratings.get(p.paper_id))
        similar.append(sp)

    rating_context = build_rating_context(papers, ratings, venue_stats)

    # 신뢰도는 retrieval_node가 이미 판정해 state에 넣어 뒀다 (재정렬이 그 값을
    # 보고 돌릴지 정해야 해서 검색 단계로 옮겼다). 여기서 다시 계산하면 두 곳이
    # 갈라질 수 있다.
    confidence = state.get("confidence") or RetrievalConfidence()

    # 근거 풀은 **선정 5편의 리뷰 원문**에서 만든다. 집계에서 뽑으면 화면에 없는
    # 논문의 문장이 근거로 붙는다. LLM을 끄더라도 채운다 — 근거 목록은 생성 결과가
    # 아니라 조회 결과다.
    selected = state.get("selected_papers", [])
    if selected:
        evidence_pool = build_evidence_for_selected(
            selected, state.get("selection_points", {}))
    else:
        # 재정렬을 못 돌린 경우(weak·LLM off·PDF 없음)에도 근거는 남긴다.
        evidence_pool = build_evidence(patterns, papers)

    summary, citations = _summary(state, selected, patterns, trends,
                                  rating_context, confidence, evidence_pool, llm)

    report = Report(
        query_title=state["query_title"],
        query_abstract=state.get("query_abstract", ""),
        confidence=confidence,
        similar_papers=similar,
        selected_papers=selected,
        review_patterns=patterns,
        venue_trends=trends,
        rating_context=rating_context,
        resubmission_flows=state.get("resubmission_flows", []),
        evidence=evidence_pool,
        citations=citations,
        summary_markdown=summary,
    )
    return {"report": report}


def _risky_first(patterns, significant_only: bool = True):
    """당락 격차가 큰 지적 순. accept율 차이가 곧 '치명도'다.

    기본은 Fisher 검정을 통과한 것만 — 이웃 20편에서 "4편 중 0편 통과" 같은
    표본은 그럴듯해 보이지만 우연이다 (§18).
    """
    scored = [(p, p.accept_rate_without - p.accept_rate_with)
              for p in patterns
              if p.accept_rate_with is not None and p.accept_rate_without is not None
              and (p.is_contrast_significant or not significant_only)]
    return sorted(scored, key=lambda kv: kv[1], reverse=True)


def _rating_lines(rc) -> list[str]:
    """rating 맥락을 사람이 읽는 줄로. 편향 venue는 반드시 경고를 단다."""
    if not rc or not rc.rated_papers:
        return []
    lines = [f"- 이웃 평균 점수 {rc.neighbor_mean} ({rc.rated_papers}편)"]
    if rc.accepted_mean is not None and rc.rejected_mean is not None:
        lines[-1] += f" — 통과 {rc.accepted_mean} vs 탈락 {rc.rejected_mean}"
    if rc.threshold is not None:
        lines.append(f"- 당락 경계: {rc.threshold_venue} 기준 평균 "
                     f"{rc.threshold} 이상이면 통과율 50% 초과")
    if rc.split_papers:
        lines.append(f"- 리뷰어 의견이 갈린 논문 {len(rc.split_papers)}편: "
                     f"{rc.split_papers[0][:50]}")
    if rc.biased_venues:
        lines.append(f"- ⚠️ {', '.join(rc.biased_venues)}는 채택 논문 위주로만 "
                     f"공개되어 accept율을 실제 채택률로 읽으면 안 됨")
    return lines


def _first_label(evidence_pool, aspect: str) -> str:
    """해당 aspect의 첫 근거 라벨. 없으면 빈 문자열 (스텁 요약이 인용할 때 쓴다)."""
    for e in evidence_pool:
        if e.aspect == aspect:
            return f" [{e.label}]"
    return ""


def _summary(state, selected, patterns, trends, rating_context, confidence,
             evidence_pool, llm) -> tuple[str, list[str]]:
    """(요약 마크다운, 실제로 인용된 근거 라벨) 반환.

    **주어는 항상 유사 논문이다.** "당신은 X를 지적받을 것입니다"가 아니라 "비슷한
    논문들은 이런 지적을 받았습니다" — 지적 범주 *예측*은 '검색 없음' 베이스라인에
    졌고(설계서 §24), 잘 되는 것은 구체적인 지적 *문장*을 근거로 가져오는 쪽이다.
    이 경계를 넘으면 검증되지 않은 것을 파는 것이 된다.
    """
    if not selected:
        return _no_selection_summary(confidence), []

    if llm is None:
        return _stub_summary(selected, evidence_pool, confidence)

    facts = {
        "query": state["query_title"],
        "retrieval_confidence": confidence.level,
        "papers": [{
            "title": p.title,
            "venue": p.venue,
            "year": p.year,
            "decision": p.decision,
            "why_selected": p.reason,
            "selection_confidence": p.confidence,
            "avg_rating": p.avg_rating,
            "rating_spread": p.rating_spread,
        } for p in selected],
        # 검색된 **원문**. 이 항목이 이 요약을 검색 증강 생성(RAG)으로 만든다.
        "evidence": format_for_prompt(evidence_pool),
    }
    system = (
        "You are a research assistant. A researcher uploaded their paper. We found "
        "the papers below to be genuinely similar to it, and we have the reviews those "
        "papers actually received. Write a concise Korean markdown briefing "
        "(under 250 words) about WHAT REVIEWERS SAID ABOUT THESE PAPERS.\n"
        "\n"
        "THE ONE RULE: the subject of every sentence is the similar papers, never the "
        "reader's paper. Write '비슷한 논문 3편이 실험 규모를 지적받았습니다', never "
        "'당신의 논문은 실험 규모를 지적받을 것입니다'. We have not analysed the "
        "reader's paper and cannot predict its reviews — saying otherwise sells "
        "something we did not measure. Do not write '예상 지적', '받을 것으로 "
        "보입니다', or any forward-looking phrasing about the reader's work.\n"
        "\n"
        "FIRST, check retrieval_confidence. If 'weak', say plainly in the first line "
        "that the corpus has essentially no papers on this topic and keep the rest to "
        "two sentences. If 'moderate', note the matches come from adjacent fields.\n"
        "\n"
        "Otherwise cover, in this order: what these papers have in common with the "
        "reader's work (use why_selected — it is the judgement that picked them); what "
        "reviewers criticised, concretely; and how those papers fared.\n"
        "\n"
        "Be specific. '베이스라인 비교가 부족하다는 지적' is a category and says almost "
        "nothing — the corpus-wide rate for that is 78.8%, so it is closer to a constant "
        "than a finding. Name the actual objection: 'QM9 하나로만 평가했다는 지적'. The "
        "evidence array is where those sentences are.\n"
        "\n"
        "GROUNDING: `evidence` holds VERBATIM text retrieved from the corpus — real "
        "reviewer criticisms (kind=review_point) and area-chair meta-reviews "
        "(kind=meta_review). When a sentence rests on a specific criticism, append that "
        "item's label in brackets: [E1], or [E1, E2]. Cite ONLY labels present in "
        "`evidence` — an invented label is stripped and the sentence ends up looking "
        "unsupported. Paraphrase in Korean; do not quote at length.\n"
        "A citation is a specific claim: that THIS item's text supports THIS sentence, "
        "about THIS item's paper. Before appending a label, re-read the item — does it "
        "actually say what your sentence says, about the paper your sentence names? If "
        "not, drop the label or rewrite the sentence. A real label on a sentence it does "
        "not support is worse than no citation, because it looks verified.\n"
        "Every E* item is a CRITICISM. The corpus records no strengths at all, so you "
        "have no evidence for any claim that something is a strength or is well done — "
        "do not make such claims, and never attach a citation to one.\n"
        "\n"
        "Ratings: avg_rating scales differ by venue and year (ICLR 2020 was 1-8, others "
        "1-10), so never compare raw scores across papers. A large rating_spread means "
        "reviewers disagreed — that is worth saying; the number itself is not.\n"
        "Do NOT compute an acceptance rate from these papers. Five papers chosen for "
        "similarity are not a sample of anything.\n"
        "\n"
        "WRITING FOR THE READER: the input keys (why_selected, rating_spread, "
        "selection_confidence, retrieval_confidence …) are the shape of the data handed "
        "to you, not vocabulary for the reader — they have never seen the JSON. "
        "Bracketed citation labels are the sole exception; keep those verbatim.\n"
        "LENGTH: about 250 words. Achieve it by covering fewer things, not by "
        "compressing sentences into fragments. Every sentence stays complete and "
        "readable.\n"
        "Do not invent facts.")
    raw = llm.text(SONNET, system, json.dumps(facts, ensure_ascii=False),
                   max_tokens=SONNET_MAX_TOKENS,
                   thinking={"type": "adaptive"},
                   output_config={"effort": SONNET_EFFORT})
    # 모델이 지어낸 라벨은 여기서 제거된다 — 남은 인용은 전부 실제 원문을 가리킨다.
    return validate_citations(raw, evidence_pool)


def _no_selection_summary(confidence) -> str:
    """고른 논문이 없을 때. **후보 풀을 대신 보여주지 않는다.**

    비슷한 논문이 없다는 것이 이 경우의 정직한 답이고, 후보로 메우면 2단계를 넣은
    이유가 통째로 무효가 된다.
    """
    if not confidence.is_reliable:
        return (f"> ⚠️ **{confidence.message}**\n\n"
                "비슷한 논문을 찾지 못해 보여드릴 리뷰가 없습니다.")
    return ("비슷하다고 판단할 만한 논문을 찾지 못했습니다. 검색에는 걸렸지만 "
            "본문을 대조한 결과 같은 문제를 다루는 논문이 아니었습니다.")


def _stub_summary(selected, evidence_pool, confidence) -> tuple[str, list[str]]:
    """LLM 없이 조회 결과만으로 만드는 결정론적 요약 ($0, 배선 검증용).

    스텁도 근거 라벨을 단다 — LLM을 꺼도 "이 결론의 출처"가 추적되어야 경로가
    하나로 유지된다 (validate_citations를 같이 통과시킨다).
    """
    lines = []
    if not confidence.is_reliable:
        lines.append(f"> ⚠️ **{confidence.message}**\n")
    lines.append(f"## 비슷한 논문 {len(selected)}편이 받은 리뷰")
    by_paper: dict[int, list[str]] = {}
    for e in evidence_pool:
        if e.kind == "review_point":
            by_paper.setdefault(e.paper_id, []).append(e.label)
    for p in selected:
        labels = by_paper.get(p.paper_id, [])
        cite = f" [{', '.join(labels)}]" if labels else ""
        rating = (f", 평균 {p.avg_rating}점/{p.rating_count}건"
                  if p.avg_rating is not None else "")
        lines.append(f"- **{p.title}** ({p.venue}, {p.decision}{rating}){cite}")
        lines.append(f"  - 선정 이유: {p.reason}")
    return validate_citations("\n".join(lines), evidence_pool)
