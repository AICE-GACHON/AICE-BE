"""리뷰 점수 집계 로직 테스트 (DB 불필요 — ratings.py는 순수 함수)."""
from dataclasses import dataclass

from paper_assistant.graph.ratings import attach_paper_ratings, build_rating_context
from paper_assistant.db.stats import VenueStat, conference_of
from paper_assistant.schemas import SimilarPaper


@dataclass
class FakeResult:
    """SearchResult 중 rating 집계가 쓰는 필드만."""
    paper_id: int
    title: str
    venue: str
    decision: str


def _stat(venue, mean=5.15, threshold=6.0, biased=False, scale=10.0):
    return VenueStat(venue=venue, papers=1000, accept_rate=0.32,
                     rating_mean=mean, rating_sd=1.2,
                     accept_rating_mean=6.46, reject_rating_mean=4.80,
                     scale_max=scale, threshold_50=threshold,
                     is_coverage_biased=biased)


ICLR = _stat("ICLR 2025")
NEURIPS = _stat("NeurIPS 2024", mean=5.87, threshold=None, biased=True)
STATS = {"ICLR 2025": ICLR, "NeurIPS 2024": NEURIPS}


def _paper(pid=1, venue="ICLR 2025", decision="reject"):
    return SimilarPaper(paper_id=pid, openreview_id="x", title="T", venue=venue,
                        year=2025, decision=decision, rank=1)


# ------------------------------------------------- 논문별 rating 부착

def test_attach_computes_venue_relative_offsets():
    p = _paper()
    attach_paper_ratings(p, ICLR, {"avg": 6.5, "count": 4, "spread": 2.0})
    assert p.avg_rating == 6.5 and p.rating_count == 4 and p.rating_spread == 2.0
    assert p.rating_vs_venue == 1.35        # 6.5 - 5.15
    assert p.rating_vs_threshold == 0.5     # 6.5 - 6.0


def test_attach_skips_threshold_for_biased_venue():
    # 편향 venue는 threshold_50이 None이라 경계 대비를 내지 않는다
    p = _paper(venue="NeurIPS 2024")
    attach_paper_ratings(p, NEURIPS, {"avg": 6.0, "count": 4, "spread": 1.0})
    assert p.rating_vs_venue is not None
    assert p.rating_vs_threshold is None


def test_attach_is_noop_without_rating():
    p = _paper()
    attach_paper_ratings(p, ICLR, None)
    attach_paper_ratings(p, ICLR, {"avg": None})
    assert p.avg_rating is None and p.rating_count == 0


def test_attach_without_venue_stat_keeps_raw_only():
    p = _paper()
    attach_paper_ratings(p, None, {"avg": 6.5, "count": 3, "spread": 1.0})
    assert p.avg_rating == 6.5
    assert p.rating_vs_venue is None and p.rating_vs_threshold is None


# ----------------------------------------------------- 이웃 점수 맥락

def test_context_splits_accepted_and_rejected_means():
    papers = [FakeResult(1, "A", "ICLR 2025", "accept-poster"),
              FakeResult(2, "B", "ICLR 2025", "accept-oral"),
              FakeResult(3, "C", "ICLR 2025", "reject")]
    ratings = {1: {"avg": 6.0, "count": 4, "spread": 1.0},
               2: {"avg": 7.0, "count": 4, "spread": 1.0},
               3: {"avg": 4.0, "count": 4, "spread": 1.0}}
    ctx = build_rating_context(papers, ratings, STATS)
    assert ctx.rated_papers == 3
    assert ctx.accepted_mean == 6.5 and ctx.rejected_mean == 4.0
    assert abs(ctx.neighbor_mean - 5.67) < 0.01


def test_context_threshold_from_most_common_unbiased_venue():
    papers = [FakeResult(1, "A", "NeurIPS 2024", "accept-poster"),
              FakeResult(2, "B", "NeurIPS 2024", "accept-poster"),
              FakeResult(3, "C", "ICLR 2025", "reject")]
    ratings = {i: {"avg": 6.0, "count": 4, "spread": 1.0} for i in (1, 2, 3)}
    ctx = build_rating_context(papers, ratings, STATS)
    # NeurIPS가 더 많지만 편향 venue라 경계가 없다 → ICLR 경계를 쓴다
    assert ctx.threshold == 6.0 and ctx.threshold_venue == "ICLR 2025"


def test_context_reports_biased_venues_present_in_neighborhood():
    papers = [FakeResult(1, "A", "NeurIPS 2024", "accept-poster"),
              FakeResult(2, "B", "ICLR 2025", "reject")]
    ratings = {1: {"avg": 6.0, "count": 4, "spread": 1.0},
               2: {"avg": 5.0, "count": 4, "spread": 1.0}}
    ctx = build_rating_context(papers, ratings, STATS)
    assert ctx.biased_venues == ["NeurIPS 2024"]     # ICLR은 포함되지 않는다


def test_context_flags_only_widely_split_papers():
    papers = [FakeResult(1, "갈린 논문", "ICLR 2025", "reject"),
              FakeResult(2, "합의된 논문", "ICLR 2025", "reject")]
    ratings = {1: {"avg": 5.0, "count": 4, "spread": 6.0},   # 1점~7점
               2: {"avg": 5.0, "count": 4, "spread": 1.0}}
    ctx = build_rating_context(papers, ratings, STATS)
    assert ctx.split_papers == ["갈린 논문"]


def test_context_orders_split_papers_by_spread():
    papers = [FakeResult(i, f"P{i}", "ICLR 2025", "reject") for i in (1, 2, 3)]
    ratings = {1: {"avg": 5.0, "count": 4, "spread": 4.0},
               2: {"avg": 5.0, "count": 4, "spread": 7.0},
               3: {"avg": 5.0, "count": 4, "spread": 5.0}}
    ctx = build_rating_context(papers, ratings, STATS)
    assert ctx.split_papers == ["P2", "P3", "P1"]


def test_context_empty_without_ratings():
    papers = [FakeResult(1, "A", "ICLR 2025", "reject")]
    ctx = build_rating_context(papers, {}, STATS)
    assert ctx.rated_papers == 0 and ctx.neighbor_mean is None
    assert ctx.threshold is None and ctx.split_papers == []


def test_context_survives_missing_venue_stats():
    papers = [FakeResult(1, "A", "ICLR 2099", "reject")]
    ratings = {1: {"avg": 5.0, "count": 4, "spread": 1.0}}
    ctx = build_rating_context(papers, ratings, {})
    assert ctx.rated_papers == 1 and ctx.neighbor_mean == 5.0
    assert ctx.threshold is None and ctx.biased_venues == []


def test_conference_of():
    assert conference_of("ICLR 2024") == "ICLR"
    assert conference_of("NeurIPS 2024") == "NeurIPS"
    assert conference_of("") == ""
