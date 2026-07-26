"""재투고 흐름 매칭 — 같은 논문의 복수 투고 기록 연결 (설계 §6).

우선순위 폴백 체인 (높은 신뢰도부터):
  1. arXiv ID 일치           confidence 1.00  (현재 arxiv_id가 NULL이라 no-op,
                                              S2 보강 후 자동 활성화)
  2. 정규화 제목 정확 일치    confidence 0.95
  3. 제목 유사(trgm) + 저자 겹침(Jaccard ≥ 0.5)  confidence = 제목 유사도

결과는 submission_links(earlier→later)에 upsert. 재실행 안전(멱등).
LLM 불필요. 전체 수집 완료 후 재실행하면 커버리지가 올라간다.

사용법:
    python -m paper_assistant.ingest.submission_linker
"""
import logging
import re

from paper_assistant.db.connection import cursor

log = logging.getLogger("linker")

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
TITLE_SIM_THRESHOLD = 0.7    # trgm 제목 유사도 하한
AUTHOR_JACCARD_MIN = 0.5     # 저자 집합 겹침 하한


def normalize_title(title: str) -> str:
    """소문자화 + 영숫자 외 제거 + 공백 정리. 'Deep Learning!' → 'deep learning'."""
    return _NON_ALNUM.sub(" ", (title or "").lower()).strip()


def venue_sort_key(venue: str, year: int) -> tuple[int, int]:
    """투고 시점 순서용 키. 같은 해면 ICLR(상반기 통보) < NeurIPS(하반기).

    earlier = 더 작은 키. ICLR 2024 reject → NeurIPS 2024 accept 흐름을 만든다.
    """
    conf = venue.split()[0] if venue else ""
    within = 0 if conf == "ICLR" else 1
    return (year, within)


def author_jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _order(p_early, p_late):
    """(earlier_id, later_id)로 정렬. 두 논문 dict는 id/venue/year 포함."""
    ka = venue_sort_key(p_early["venue"], p_early["year"])
    kb = venue_sort_key(p_late["venue"], p_late["year"])
    if ka <= kb:
        return p_early["id"], p_late["id"]
    return p_late["id"], p_early["id"]


def _fetch_papers() -> list[dict]:
    with cursor() as cur:
        cur.execute("SELECT id, arxiv_id, title, venue, year FROM papers")
        return [{"id": r[0], "arxiv_id": r[1], "title": r[2],
                 "venue": r[3], "year": r[4]} for r in cur.fetchall()]


def _author_sets(paper_ids: list[int]) -> dict[int, set]:
    """paper_id → 저자 openreview_id 집합."""
    if not paper_ids:
        return {}
    with cursor() as cur:
        cur.execute(
            """
            SELECT pa.paper_id, a.openreview_id
            FROM paper_authors pa JOIN authors a ON a.id = pa.author_id
            WHERE pa.paper_id = ANY(%s) AND a.openreview_id IS NOT NULL
            """,
            (paper_ids,))
        out: dict[int, set] = {}
        for pid, oid in cur.fetchall():
            out.setdefault(pid, set()).add(oid)
    return out


def _different_submission(p1: dict, p2: dict) -> bool:
    """서로 다른 투고(venue+year)인가. 같은 venue+year는 재투고가 아니다."""
    return (p1["venue"], p1["year"]) != (p2["venue"], p2["year"])


def find_links() -> list[tuple[int, int, str, float]]:
    """모든 재투고 링크 후보를 폴백 체인으로 수집. (earlier, later, method, conf)."""
    papers = _fetch_papers()
    by_id = {p["id"]: p for p in papers}
    links: dict[frozenset, tuple] = {}   # {id1,id2} -> (early, late, method, conf)

    def add(p1, p2, method, conf):
        if not _different_submission(p1, p2):
            return
        key = frozenset((p1["id"], p2["id"]))
        if key in links:
            return  # 이미 더 높은 우선순위로 연결됨
        early, late = _order(p1, p2)
        links[key] = (early, late, method, conf)

    # 1) arXiv ID (현재 대부분 NULL — S2 보강 후 활성화)
    by_arxiv: dict[str, list[dict]] = {}
    for p in papers:
        if p["arxiv_id"]:
            by_arxiv.setdefault(p["arxiv_id"], []).append(p)
    for group in by_arxiv.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                add(group[i], group[j], "arxiv_id", 1.0)

    # 2) 정규화 제목 정확 일치
    by_title: dict[str, list[dict]] = {}
    for p in papers:
        nt = normalize_title(p["title"])
        if nt:
            by_title.setdefault(nt, []).append(p)
    for group in by_title.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                add(group[i], group[j], "title_exact", 0.95)

    # 3) 제목 유사(trgm) + 저자 겹침
    with cursor() as cur:
        cur.execute("SELECT set_limit(%s::real)", (TITLE_SIM_THRESHOLD,))
        cur.execute(
            """
            SELECT a.id, b.id, similarity(a.title, b.title) AS sim
            FROM papers a JOIN papers b
              ON a.id < b.id AND a.title %% b.title
            WHERE (a.venue, a.year) <> (b.venue, b.year)
            """, ())   # 빈 파라미터 → psycopg가 %%를 %로 축약
        fuzzy = cur.fetchall()

    cand_ids = {r[0] for r in fuzzy} | {r[1] for r in fuzzy}
    authors = _author_sets(list(cand_ids))
    for id1, id2, sim in fuzzy:
        key = frozenset((id1, id2))
        if key in links:            # 이미 arxiv/exact로 잡힘
            continue
        jac = author_jaccard(authors.get(id1, set()), authors.get(id2, set()))
        if jac >= AUTHOR_JACCARD_MIN:
            add(by_id[id1], by_id[id2], "title_author_fuzzy", round(float(sim), 3))

    return list(links.values())


def run_linking() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    links = find_links()

    with cursor(commit=True) as cur:
        cur.execute("TRUNCATE submission_links")   # 전량 재계산 (멱등)
        for early, late, method, conf in links:
            cur.execute(
                """
                INSERT INTO submission_links
                    (earlier_paper_id, later_paper_id, match_method, confidence)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (earlier_paper_id, later_paper_id) DO UPDATE SET
                    match_method = EXCLUDED.match_method,
                    confidence = EXCLUDED.confidence
                """,
                (early, late, method, conf))

    by_method: dict[str, int] = {}
    for _, _, m, _c in links:
        by_method[m] = by_method.get(m, 0) + 1
    log.info("재투고 링크 %d건: %s", len(links), by_method)
    return len(links)


if __name__ == "__main__":
    run_linking()
