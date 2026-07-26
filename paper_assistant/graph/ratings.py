"""리뷰 점수(rating) 집계 — 이웃 분포 + 당락 기준선.

rating은 코퍼스에 168,217건 100% 커버리지로 있으면서 여태 Report에 한 번도
노출되지 않았다. 당락을 가장 잘 가르는 단일 신호다 (설계서 §19):
코퍼스 전체 accept 평균 6.24 vs reject 4.71.

다만 원점수를 그대로 주면 오해를 부른다:
  - 척도가 다르다 (ICLR 2020은 1~8, 나머지 1~10)
  - venue별 평균이 다르다 (ICLR 2025 5.15 vs NeurIPS 2021 6.31)
  - NeurIPS는 코퍼스의 95%가 accept라 accept율·경계를 신뢰할 수 없다
따라서 항상 **venue 기준선 대비**로 환산해서 내보낸다.

DB를 모르는 순수 함수라 테스트가 쉽다 — 조회는 nodes.py가 한다.
"""
from paper_assistant.graph.venue_stats import VenueStat
from paper_assistant.schemas import RatingContext

# 리뷰어 의견이 '갈렸다'고 볼 점수 차이 (10점 척도에서 4점 이상은 확연한 대립)
SPLIT_SPREAD = 4.0
MAX_SPLIT_EXAMPLES = 3


def attach_paper_ratings(paper, stat: VenueStat | None,
                         rating: dict | None) -> None:
    """SimilarPaper에 rating 필드를 채운다 (venue 기준선 대비 포함).

    rating: {"avg", "count", "spread"} 또는 None
    """
    if not rating or rating.get("avg") is None:
        return
    paper.avg_rating = round(rating["avg"], 2)
    paper.rating_count = rating.get("count", 0)
    if rating.get("spread") is not None:
        paper.rating_spread = round(rating["spread"], 1)

    if stat is None:
        return
    if stat.rating_mean is not None:
        paper.rating_vs_venue = round(paper.avg_rating - stat.rating_mean, 2)
    # 편향 venue는 threshold_50이 None이라 자연히 건너뛴다
    if stat.threshold_50 is not None:
        paper.rating_vs_threshold = round(paper.avg_rating - stat.threshold_50, 2)


def build_rating_context(papers, ratings: dict[int, dict],
                         stats: dict[str, VenueStat]) -> RatingContext:
    """이웃 전체의 점수 분포 + 당락 경계 + 편향 경고를 조립한다.

    papers: SearchResult 리스트 (venue/decision 보유)
    ratings: {paper_id: {"avg", "count", "spread"}}
    """
    ctx = RatingContext()

    scored = [(p, ratings[p.paper_id]) for p in papers
              if ratings.get(p.paper_id, {}).get("avg") is not None]
    if not scored:
        return ctx

    ctx.rated_papers = len(scored)
    ctx.neighbor_mean = round(sum(r["avg"] for _, r in scored) / len(scored), 2)

    accepted = [r["avg"] for p, r in scored if p.decision.startswith("accept")]
    rejected = [r["avg"] for p, r in scored if p.decision == "reject"]
    if accepted:
        ctx.accepted_mean = round(sum(accepted) / len(accepted), 2)
    if rejected:
        ctx.rejected_mean = round(sum(rejected) / len(rejected), 2)

    # 당락 경계: 이웃에 가장 많이 등장한 '편향 없는' venue의 값을 쓴다.
    # 여러 venue가 섞여 있어도 척도가 다른 값을 평균내는 것보다 낫다.
    counts: dict[str, int] = {}
    for p, _ in scored:
        stat = stats.get(p.venue)
        if stat and stat.threshold_50 is not None:
            counts[p.venue] = counts.get(p.venue, 0) + 1
    if counts:
        top_venue = max(counts, key=lambda v: (counts[v], v))
        ctx.threshold = stats[top_venue].threshold_50
        ctx.threshold_venue = top_venue

    # 리뷰어 의견이 갈린 논문 — "이 주제는 평가가 엇갈린다"는 신호
    split = sorted(
        (r["spread"], p.title) for p, r in scored
        if r.get("spread") is not None and r["spread"] >= SPLIT_SPREAD)
    ctx.split_papers = [title for _, title in reversed(split)][:MAX_SPLIT_EXAMPLES]

    # 표본 편향 venue 경고 (이웃에 실제로 등장한 것만)
    ctx.biased_venues = sorted({
        p.venue for p, _ in scored
        if (s := stats.get(p.venue)) is not None and s.is_coverage_biased})
    return ctx
