"""LangGraph 노드 6종.

지능이 필요한 노드(태깅·종합)만 LLM을 쓰고, 나머지는 코드로 처리한다.
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
    build_evidence, format_for_prompt, validate_citations)
from paper_assistant.graph.llm import (
    HAIKU, SONNET, SONNET_EFFORT, SONNET_MAX_TOKENS)
from paper_assistant.graph.ratings import attach_paper_ratings, build_rating_context
from paper_assistant.graph.state import PipelineState
from paper_assistant.db.stats import (
    conference_of, load_base_rates, load_venue_stats)
from paper_assistant.retrieval.hybrid_search import hybrid_search
from paper_assistant.schemas import (
    Report, ResubmissionFlow, RetrievalConfidence, ReviewPattern, SimilarityTag,
    SimilarPaper, VenueTrend)

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
    """SPECTER2 임베딩 → 하이브리드 검색 top-K."""
    title = state["query_title"]
    abstract = state.get("query_abstract", "")
    qvec = embedder.encode_one(title, abstract).numpy()
    results = hybrid_search(qvec, f"{title} {abstract}", top_k=20)
    return {"query_embedding": qvec.tolist(), "similar_papers": results}


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

    # 코사인은 벡터 검색에 걸린 논문만 가진다 (FTS-only는 None).
    # 순위 오름차순으로 정렬해야 상위 k개를 제대로 고른다.
    ranked_cos = [p.cosine for p in sorted(
        (p for p in papers if p.cosine is not None and p.vector_rank is not None),
        key=lambda p: p.vector_rank)]
    level, evidence_score = retrieval_confidence(ranked_cos)
    confidence = RetrievalConfidence(
        level=level, evidence=round(evidence_score, 4),
        is_reliable=level != "weak",
        message=CONFIDENCE_MESSAGES[level])

    # 검색된 원문(리뷰 지적 문장 + AC 총평)을 인용 가능한 근거 풀로 만든다.
    # LLM을 끄더라도 채운다 — 근거 목록은 생성 결과가 아니라 검색 결과다.
    evidence_pool = build_evidence(patterns, papers)

    summary, citations = _summary(state, similar, patterns, trends,
                                  rating_context, confidence, evidence_pool, llm)

    report = Report(
        query_title=state["query_title"],
        query_abstract=state.get("query_abstract", ""),
        confidence=confidence,
        similar_papers=similar,
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


def _summary(state, similar, patterns, trends, rating_context, confidence,
             evidence_pool, llm) -> tuple[str, list[str]]:
    """(요약 마크다운, 실제로 인용된 근거 라벨) 반환."""
    distinctive = [p for p in patterns if p.is_distinctive]
    risky = _risky_first(patterns)
    rc = rating_context

    if llm is None:
        # 스텁: 구조화 데이터로 결정론적 요약
        lines = [f"## 유사 논문 {len(similar)}편"]
        # 신뢰할 수 없는 결과면 맨 앞에서 알린다 — 뒤에 묻으면 아무도 안 읽는다
        if not confidence.is_reliable:
            lines.insert(0, f"> ⚠️ **{confidence.message}**\n")
        # 스텁도 근거 라벨을 단다 — LLM을 꺼도 "이 결론의 출처"가 추적된다.
        if distinctive:
            lines.append("- 이 주제 특유의 지적: " + ", ".join(
                f"{p.label}({p.paper_count}/{p.total_papers}, lift {p.lift})"
                f"{_first_label(evidence_pool, p.aspect)}"
                for p in distinctive[:3]))
        else:
            lines.append("- 이 주제 특유의 지적 없음 (전부 코퍼스 평균 수준)")
        if risky:
            p, _ = risky[0]
            lines.append(
                f"- 당락에 실제로 영향: {p.label} — 지적받은 {p.decided_with}편 중 "
                f"{p.accept_with}편 통과({p.accept_rate_with*100:.0f}%) vs "
                f"미지적 {p.decided_without}편 중 {p.accept_without}편"
                f"({p.accept_rate_without*100:.0f}%), p={p.contrast_p_value}"
                f"{_first_label(evidence_pool, p.aspect)}")
        else:
            lines.append("- 당락 격차가 유의한 지적 없음 (이웃 표본이 작아 판단 보류)")
        lines += _rating_lines(rc)
        if trends:
            lines.append("- 게재 경향: " + ", ".join(
                f"{t.venue} {t.accept_count}/{t.paper_count}"
                + (" (표본 편향)" if t.is_coverage_biased else "")
                for t in trends[:3]))
        # 스텁이 만든 라벨도 같은 검증을 통과시킨다 (경로를 하나로 유지).
        return validate_citations("\n".join(lines), evidence_pool)

    facts = {
        "query": state["query_title"],
        "retrieval_confidence": confidence.level,
        "similar_papers": [{"title": s.title, "venue": s.venue,
                            "decision": s.decision, "match_type": s.match_type}
                           for s in similar[:10]],
        # 검색된 **원문**. 통계만 주면 "베이스라인 비교 부족" 같은 라벨을 되풀이할
        # 뿐이라, 실제 리뷰 문장과 AC 총평을 함께 넣어 구체적으로 쓰게 한다.
        # 이 항목이 이 파이프라인을 검색 증강 생성(RAG)으로 만드는 지점이다.
        "evidence": format_for_prompt(evidence_pool),
        # lift와 당락 대조를 같이 넘긴다. 빈도만 주면 모델이 "베이스라인 비교를
        # 강화하세요" 같은 코퍼스 상수를 결론처럼 써버린다 (§18).
        "review_patterns": [{
            "label": p.label,
            "observed": f"{p.paper_count}/{p.total_papers}",
            "corpus_base_rate": p.base_rate,
            "lift": p.lift,
            "distinctive": p.is_distinctive,
            "accept_rate_if_criticized": p.accept_rate_with,
            "accept_rate_if_not": p.accept_rate_without,
            "contrast_sample": f"{p.decided_with} vs {p.decided_without}",
            "contrast_significant": p.is_contrast_significant,
        } for p in patterns],
        "venue_trends": [{
            "venue": t.venue,
            "accept": f"{t.accept_count}/{t.paper_count}",
            "corpus_accept_rate": t.corpus_accept_rate,
            "accept_lift": t.accept_lift,
            "coverage_biased": t.is_coverage_biased,
        } for t in trends],
        "ratings": {
            "neighbor_mean": rc.neighbor_mean,
            "accepted_mean": rc.accepted_mean,
            "rejected_mean": rc.rejected_mean,
            "rated_papers": rc.rated_papers,
            "accept_threshold": rc.threshold,
            "threshold_venue": rc.threshold_venue,
            "reviewer_split_papers": rc.split_papers,
            "coverage_biased_venues": rc.biased_venues,
        } if rc and rc.rated_papers else None,
    }
    system = (
        "You are a research assistant. Given structured findings about papers similar "
        "to a query, write a concise Korean markdown briefing (under 250 words).\n"
        "FIRST, check retrieval_confidence. If it is 'weak', the corpus has no papers "
        "on this topic and every finding below is noise — say so in the first line and "
        "keep the rest to two sentences. If 'moderate', note that matches are from "
        "adjacent fields before continuing. Never present weak-confidence results as "
        "real findings.\n"
        "Otherwise cover: "
        "what similar work exists, which review criticisms are DISTINCTIVE to this topic, "
        "and where such papers get published. Be concrete; cite the counts.\n"
        "Critical: a criticism with lift near 1.0 is the corpus-wide norm for ML papers "
        "(e.g. 79% of all papers are criticized on baselines) — never present it as a "
        "finding about this topic. Lead with distinctive=true patterns, and with "
        "criticisms whose accept_rate_if_criticized is far below accept_rate_if_not. "
        "Only state an accept-rate gap as a conclusion when contrast_significant is true; "
        "otherwise the sample is too small — mention it as tentative or omit it. "
        "If nothing is distinctive, say so plainly rather than manufacturing an "
        "insight.\n"
        "The two measures answer different questions and are independent. Lift compares "
        "this neighbourhood's criticism rate against the whole corpus (is this topic "
        "unusual?). The accept-rate contrast compares criticised vs uncriticised papers "
        "WITHIN this neighbourhood (did this criticism cost them?). A criticism can be "
        "corpus-typical and still separate accept from reject here. Never explain one "
        "measure with the other, and never claim an accept-rate gap is or is not typical "
        "of the corpus — you are given no corpus-wide contrast data, so that comparison "
        "cannot be made.\n"
        "Review scores: state the neighborhood mean and, when accept_threshold is "
        "given, what score this topic needs to clear. Never compare raw scores "
        "across different venues — the scales differ.\n"
        "Any venue in coverage_biased_venues publishes reviews mainly for ACCEPTED "
        "papers, so its accept rate is a sampling artifact, not a real acceptance "
        "rate — never tell the user that venue is easier to get into. Use accept_lift "
        "(neighborhood vs that venue's own corpus) for relative statements instead.\n"
        "GROUNDING: the `evidence` array holds VERBATIM text retrieved from the "
        "corpus — real reviewer criticisms (kind=review_point) and area-chair "
        "meta-reviews (kind=meta_review). Ground your claims in it: when a sentence "
        "rests on a specific criticism, append that item's label in brackets, e.g. "
        "[E1] or [M2]. Combine as [E1, E2]. Prefer naming the concrete objection "
        "('evaluated only on QM9') over the generic category label. "
        "Cite ONLY labels present in `evidence` — a label you invent will be stripped "
        "and the sentence will look unsupported. Statistics (counts, lift, accept "
        "rates) need no citation; they come from the aggregates above. "
        "Do not quote evidence text at length — paraphrase in Korean.\n"
        "A citation is a specific claim: that THIS item's text supports THIS sentence, "
        "about THIS item's paper. Before appending a label, re-read the item — does it "
        "actually say what your sentence says, about the paper your sentence names? If "
        "not, drop the label or rewrite the sentence. A real label on a sentence it does "
        "not support is worse than no citation at all, because it looks verified.\n"
        "Every E* item is a CRITICISM a reviewer raised. The corpus records no strengths "
        "at all, so the pool contains none and you have no evidence for any claim that "
        "something is a strength, is well done, or is exemplary — do not make such claims, "
        "and never attach a citation to one.\n"
        "WRITING FOR THE READER: the input keys above (distinctive, lift, "
        "contrast_significant, accept_lift, coverage_biased, corpus, corpus-wide, "
        "base_rate …) are the shape of "
        "the data handed to you, not vocabulary for the reader. Write every one of them "
        "as plain Korean: '이 주제에 특히 두드러진 지적', '코퍼스 평균의 1.6배', "
        "'표본이 충분해 우연으로 보기 어렵다', '채택 논문 위주로만 공개된 학회'. "
        "The reader has never seen the JSON and cannot look a key up. Bracketed "
        "citation labels ([E1], [M2]) are the sole exception — keep those verbatim.\n"
        "LENGTH: about 250 words. Achieve it by covering fewer things, not by "
        "compressing sentences into fragments — every sentence stays complete and "
        "readable. If you must drop something, drop the detail that would not change "
        "what the reader does next.\n"
        "Do not invent facts.")
    raw = llm.text(SONNET, system, json.dumps(facts, ensure_ascii=False),
                   max_tokens=SONNET_MAX_TOKENS,
                   thinking={"type": "adaptive"},
                   output_config={"effort": SONNET_EFFORT})
    # 모델이 지어낸 라벨은 여기서 제거된다 — 남은 인용은 전부 실제 원문을 가리킨다.
    return validate_citations(raw, evidence_pool)
