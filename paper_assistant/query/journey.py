"""재투고 궤적 조회 — 같은 논문이 어디에 내서 어떻게 됐는지.

submission_links(744건)는 한때 Report에서 **집계값으로만** 쓰였다("ICLR reject →
NeurIPS accept 12건"). 그 집계(resubmission_flows)는 통계 레이어와 함께 제거됐고,
이제 이 링크를 읽는 곳은 여기뿐이다 — 개별 논문에서 "이 논문은 작년에 ICLR에서
떨어졌다가 고쳐서 NeurIPS에 붙었다"를 말해주는 경로다.

DB 조회만 하므로 빠르고, 외부 API가 죽어도 항상 채워진다 — get_paper_story가
이 부분만은 반드시 내려줄 수 있게 story.py와 분리해 뒀다.

핵심 어려움: 링크는 **인접 쌍만** 기록된다. 실측에 8346→174, 174→27997처럼
사슬이 있어서, 조회한 논문이 사슬 한가운데면 한 방향만 따라가서는 궤적의 절반이
잘린다. 그래서 양방향으로 전이 순회한다.
"""
import logging

from paper_assistant.db.connection import cursor
from paper_assistant.db.stats import load_venue_stats
from paper_assistant.schemas import JourneyStop, SubmissionJourney

log = logging.getLogger(__name__)

# 순회 상한. 정상 궤적은 2~4편이고, 제목 일치로 묶다 보면 동명 논문이 무더기로
# 엮일 수 있어(예: "Anonymous submission") 방어적으로 끊는다.
MAX_STOPS = 12

_LINKS_SQL = """
    SELECT earlier_paper_id, later_paper_id, match_method, confidence
    FROM submission_links
    WHERE earlier_paper_id = ANY(%s) OR later_paper_id = ANY(%s)
"""

_PAPERS_SQL = """
    SELECT id, openreview_id, title, venue, year, decision
    FROM papers WHERE id = ANY(%s)
"""

# 평균 rating. reviews는 100% 커버리지지만 rating 자체가 NULL인 리뷰가 있어
# AVG가 NULL을 알아서 건너뛰도록 두고, 개수는 rating이 있는 것만 센다.
_RATINGS_SQL = """
    SELECT paper_id, avg(rating), count(rating)
    FROM reviews WHERE paper_id = ANY(%s) AND rating IS NOT NULL
    GROUP BY paper_id
"""


def _collect_edges(cur, paper_id: int) -> list[tuple]:
    """paper_id와 연결된 링크를 전부 모은다 (양방향 전이 순회).

    한 홉씩 넓히면서 새 논문이 안 나올 때까지 반복한다. MAX_STOPS를 넘으면
    거기서 멈춘다 — 잘린 궤적이 잘못 묶인 무더기보다 낫다.
    """
    seen_papers = {paper_id}
    edges: dict[tuple[int, int], tuple] = {}
    frontier = [paper_id]

    while frontier and len(seen_papers) < MAX_STOPS:
        cur.execute(_LINKS_SQL, (frontier, frontier))
        rows = cur.fetchall()
        frontier = []
        for earlier, later, method, conf in rows:
            edges[(earlier, later)] = (earlier, later, method, float(conf))
            for pid in (earlier, later):
                if pid not in seen_papers:
                    seen_papers.add(pid)
                    frontier.append(pid)
        if len(seen_papers) >= MAX_STOPS:
            log.info("재투고 궤적이 %d편을 넘어 잘랐습니다 (paper_id=%s)",
                     MAX_STOPS, paper_id)
            break

    return list(edges.values())


def _order(paper_ids: set[int], edges: list[tuple],
           years: dict[int, int]) -> list[int]:
    """투고 순서대로 정렬한다.

    연도만으로는 부족하다 — ICLR 2024 → NeurIPS 2024처럼 같은 해 안에서 옮겨간
    경우가 실측에 있어 연도가 동점이 된다. 링크 방향(earlier→later)이 그때의
    유일한 근거라 위상 정렬을 먼저 하고, 순서가 안 정해지는 쌍만 연도로 가른다.
    """
    incoming = {pid: 0 for pid in paper_ids}
    outgoing: dict[int, list[int]] = {pid: [] for pid in paper_ids}
    for earlier, later, _, _ in edges:
        if earlier in outgoing and later in incoming:
            outgoing[earlier].append(later)
            incoming[later] += 1

    def _key(pid: int) -> tuple:
        return (years.get(pid, 0), pid)

    ready = sorted((p for p in paper_ids if incoming[p] == 0), key=_key)
    ordered: list[int] = []
    while ready:
        pid = ready.pop(0)
        ordered.append(pid)
        for nxt in outgoing[pid]:
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=_key)

    # 링크에 사이클이 있으면(양방향으로 잘못 매칭된 경우) 남는 논문이 생긴다.
    # 버리지 않고 연도순으로 뒤에 붙인다.
    leftover = sorted(paper_ids - set(ordered), key=_key)
    if leftover:
        log.warning("재투고 링크에 순환이 있습니다: %s", leftover)
    return ordered + leftover


def _outcome(stops: list[JourneyStop]) -> tuple[str, str | None]:
    """궤적 전체를 한 단어 + 한 문장으로 요약한다."""
    if len(stops) < 2:
        return "single", None

    first, last = stops[0], stops[-1]
    rejected_early = any(s.decision in ("reject", "desk-reject", "withdrawn")
                         for s in stops[:-1])

    if last.decision.startswith("accept") and rejected_early:
        return "improved", (
            f"{first.venue}에서 {first.decision} 뒤 {last.venue}에 다시 내서 "
            f"{last.decision}됐습니다.")
    if all(s.decision in ("reject", "desk-reject", "withdrawn") for s in stops):
        return "still_rejected", (
            f"{len(stops)}번 투고했지만 아직 통과 기록이 없습니다 "
            f"({' → '.join(s.venue for s in stops)}).")
    return "mixed", (
        f"{len(stops)}번의 투고 기록이 있습니다 "
        f"({' → '.join(f'{s.venue} {s.decision}' for s in stops)}).")


def get_submission_journey(paper_id: int) -> SubmissionJourney | None:
    """paper_id의 재투고 궤적을 조회한다. 논문이 없으면 None.

    재투고 기록이 없으면 자기 자신 1개짜리 궤적(outcome='single')을 돌려준다 —
    빈 목록으로 주면 프론트가 '조회 실패'와 구분하지 못한다.
    """
    with cursor() as cur:
        cur.execute("SELECT 1 FROM papers WHERE id = %s", (paper_id,))
        if cur.fetchone() is None:
            return None

        edges = _collect_edges(cur, paper_id)
        paper_ids = {paper_id}
        for earlier, later, _, _ in edges:
            paper_ids.update((earlier, later))
        ids = list(paper_ids)

        cur.execute(_PAPERS_SQL, (ids,))
        papers = {r[0]: r for r in cur.fetchall()}

        cur.execute(_RATINGS_SQL, (ids,))
        ratings = {r[0]: (float(r[1]), int(r[2])) for r in cur.fetchall()}

    # 조회 못 한 논문(링크가 가리키는데 papers에 없는 경우)은 궤적에서 뺀다
    paper_ids &= papers.keys()
    years = {pid: papers[pid][4] for pid in paper_ids}
    ordered = _order(paper_ids, edges, years)

    # (earlier, later) → 매칭 근거. 인접 stop 사이의 링크를 찾을 때 쓴다.
    by_pair = {(e[0], e[1]): (e[2], e[3]) for e in edges}
    venue_stats = load_venue_stats()

    stops: list[JourneyStop] = []
    for i, pid in enumerate(ordered):
        _, or_id, title, venue, year, decision = papers[pid]
        avg, count = ratings.get(pid, (None, 0))

        vs_venue = None
        stat = venue_stats.get(venue)
        if avg is not None and stat is not None and stat.rating_mean is not None:
            vs_venue = round(avg - stat.rating_mean, 2)

        # 직전 stop과 직접 연결됐을 때만 근거를 단다. 위상 정렬로 사이에 다른
        # 논문이 끼어들 수 있어, 인접했다고 링크가 있다고 단정하면 안 된다.
        method = conf = None
        if i > 0:
            method, conf = by_pair.get((ordered[i - 1], pid), (None, None))

        stops.append(JourneyStop(
            paper_id=pid, openreview_id=or_id, title=title, venue=venue,
            year=year, decision=decision,
            avg_rating=round(avg, 2) if avg is not None else None,
            rating_count=count, rating_vs_venue=vs_venue,
            is_query=(pid == paper_id),
            match_method=method, match_confidence=conf))

    outcome, message = _outcome(stops)
    return SubmissionJourney(stops=stops, outcome=outcome, message=message)
