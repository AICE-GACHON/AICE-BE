"""Semantic Scholar 보강 — s2_paper_id / 인용수 / 최종 게재처 / 저자 ID / 인용 엣지.

3단계로 나뉘고 각각 멱등(재실행 안전)이다:

  1. by-arxiv  : arxiv_id가 있는 논문을 `ARXIV:<id>`로 batch 조회 (500건/요청).
                 arxiv_matcher를 먼저 돌려야 커버리지가 올라간다.
  2. by-venue  : 그래도 s2_paper_id가 없는 논문을 venue+year 대량 조회 결과와
                 정규화 제목으로 맞춘다. S2에는 **채택된 논문만** 학회 venue로
                 등록되므로 reject/withdrawn은 여기서 거의 잡히지 않는다.
                 여기서 externalIds.ArXiv가 나오면 arxiv_id도 함께 채운다.
  3. citations : s2_paper_id끼리의 references를 코퍼스 내부 엣지로만 적재.

사용법:
    python -m paper_assistant.ingest.s2_enricher                 # 1+2
    python -m paper_assistant.ingest.s2_enricher --citations      # 3만
    python -m paper_assistant.ingest.s2_enricher --step by-venue
"""
import argparse
import logging

from paper_assistant.db.connection import cursor
from paper_assistant.ingest.arxiv_matcher import surname
from paper_assistant.ingest.s2_client import S2Client, arxiv_ref
from paper_assistant.ingest.submission_linker import normalize_title

PAPER_FIELDS = "paperId,externalIds,title,year,venue,citationCount,authors"
REF_FIELDS = "paperId,references.paperId"
REF_CHUNK = 100        # references는 응답이 커서 batch를 작게 쪼갠다
# search 엔드포인트가 통째로 막히면 (연도,alias) 조합마다 재시도 시간만 태운다.
MAX_CONSECUTIVE_FAILURES = 3

# S2의 venue 표기 (실측). 학회 약어와 정식 명칭이 섞여 있어 둘 다 조회한다.
VENUE_ALIASES = {
    "ICLR": ["ICLR", "International Conference on Learning Representations"],
    "NeurIPS": ["Neural Information Processing Systems", "NeurIPS"],
}
# 프리프린트 서버는 '최종 게재처'가 아니다.
_NOT_A_VENUE = {"", "arxiv.org", "arxiv", "corr", "openreview"}

log = logging.getLogger("s2-enrich")


# ------------------------------------------------------------------ 공통 반영

def _apply_paper(cur, paper_id: int, rec: dict, fill_arxiv: bool) -> None:
    """batch/bulk 응답 1건을 papers에 반영."""
    venue = (rec.get("venue") or "").strip()
    final_venue = venue if venue.lower() not in _NOT_A_VENUE else None
    arxiv_id = (rec.get("externalIds") or {}).get("ArXiv") if fill_arxiv else None
    cur.execute(
        """
        UPDATE papers SET
            s2_paper_id    = %s,
            citation_count = %s,
            final_venue    = coalesce(%s, final_venue),
            arxiv_id       = coalesce(arxiv_id, %s)
        WHERE id = %s
        """,
        (rec.get("paperId"), rec.get("citationCount"), final_venue, arxiv_id, paper_id))


def _apply_authors(cur, paper_id: int, rec: dict) -> int:
    """S2 저자 ID를 성(姓)으로 맞춰 authors.s2_author_id에 채운다.

    같은 성이 논문 안에 둘 이상 있으면 어느 쪽인지 확정할 수 없어 건너뛴다.
    """
    s2_authors = rec.get("authors") or []
    by_surname: dict[str, list[str]] = {}
    for a in s2_authors:
        if a.get("authorId"):
            by_surname.setdefault(surname(a.get("name", "")), []).append(a["authorId"])

    cur.execute(
        """
        SELECT a.id, a.name FROM paper_authors pa JOIN authors a ON a.id = pa.author_id
        WHERE pa.paper_id = %s AND a.s2_author_id IS NULL AND a.name IS NOT NULL
        """,
        (paper_id,))
    rows = cur.fetchall()
    filled = 0
    seen: dict[str, int] = {}
    for _aid, name in rows:
        seen[surname(name)] = seen.get(surname(name), 0) + 1
    for author_id, name in rows:
        key = surname(name)
        ids = by_surname.get(key, [])
        if len(ids) != 1 or seen.get(key, 0) != 1:
            continue
        cur.execute("UPDATE authors SET s2_author_id = %s WHERE id = %s",
                    (ids[0], author_id))
        filled += 1
    return filled


# --------------------------------------------------------------- 1. by-arxiv

def enrich_by_arxiv(client: S2Client, limit: int | None = None) -> dict:
    with cursor() as cur:
        cur.execute(
            "SELECT id, arxiv_id FROM papers "
            "WHERE arxiv_id IS NOT NULL AND s2_paper_id IS NULL ORDER BY id"
            + (" LIMIT %s" if limit else ""), (limit,) if limit else ())
        targets = cur.fetchall()
    if not targets:
        log.info("by-arxiv: 대상 없음 (arxiv_matcher를 먼저 실행했는지 확인)")
        return {"targets": 0, "found": 0}

    log.info("by-arxiv: %d편 조회", len(targets))
    recs = client.paper_batch([arxiv_ref(a) for _, a in targets], PAPER_FIELDS)

    stats = {"targets": len(targets), "found": 0, "authors": 0}
    with cursor(commit=True) as cur:
        for (paper_id, _arxiv), rec in zip(targets, recs):
            if not rec:
                continue
            _apply_paper(cur, paper_id, rec, fill_arxiv=False)
            stats["authors"] += _apply_authors(cur, paper_id, rec)
            stats["found"] += 1
    log.info("by-arxiv 결과 %s", stats)
    return stats


# --------------------------------------------------------------- 2. by-venue

def _pending_by_conf() -> dict[str, tuple[dict[str, list[int]], int, int]]:
    """학회 → ({정규화 제목: paper_id 목록}, 최소연도, 최대연도).

    연도별로 쪼개지 않는 이유(실측): S2의 publication year는 학회 연도와 어긋난다.
    ICLR 2020 venue+year=2020 슬라이스에는 실제 ICLR 2021 논문(DDIM)이 섞여 있고,
    반대로 ICLR 2020 채택 논문의 상당수는 year=2019로 색인돼 있다. 학회 단위로
    한 번에 받아 제목으로 맞추는 편이 커버리지·요청수 모두 유리하다.

    같은 제목이 두 연도에 걸쳐 있으면(= 재투고) 양쪽 모두에 같은 S2 레코드를 붙인다.
    """
    out: dict[str, tuple[dict[str, list[int]], int, int]] = {}
    with cursor() as cur:
        cur.execute("SELECT id, title, venue, year FROM papers WHERE s2_paper_id IS NULL")
        for pid, title, venue, year in cur.fetchall():
            conf = (venue or "").split()[0]
            key = normalize_title(title)
            if not conf or not key:
                continue
            titles, lo, hi = out.get(conf, ({}, year, year))
            titles.setdefault(key, []).append(pid)
            out[conf] = (titles, min(lo, year), max(hi, year))
    return out


def enrich_by_venue(client: S2Client) -> dict:
    pending = _pending_by_conf()
    stats = {"targets": sum(sum(len(v) for v in t.values())
                            for t, _lo, _hi in pending.values()),
             "found": 0, "arxiv_filled": 0, "authors": 0}
    log.info("by-venue: 미보강 %d편, 학회 %d개", stats["targets"], len(pending))

    consecutive_failures = 0
    for conf, (titles, lo, hi) in sorted(pending.items()):
        # S2 연도가 학회 연도보다 앞/뒤로 밀리는 경우가 있어 ±1년 여유를 둔다.
        # 연도 범위를 한 번에 묶으면 결과가 1만 건이 넘어 깊은 토큰 페이지네이션이
        # 필요해지는데, 그 구간에서 500/429가 연속으로 난다(실측). 연도별로 끊으면
        # 페이지가 3개 이하라 훨씬 안정적이다.
        for year in range(lo - 1, hi + 2):
            for alias in VENUE_ALIASES.get(conf, [conf]):
                found = 0
                try:
                    for page in client.search_bulk_pages(PAPER_FIELDS, venue=alias,
                                                         year=year):
                        # 페이지 단위로 반영 — 뒤쪽 페이지가 막혀도 앞의 성과는 남는다.
                        matched: list[tuple[int, dict]] = []
                        for rec in page:
                            key = normalize_title(rec.get("title") or "")
                            matched += [(pid, rec) for pid in titles.pop(key, [])]
                        with cursor(commit=True) as cur:
                            for paper_id, rec in matched:
                                _apply_paper(cur, paper_id, rec, fill_arxiv=True)
                                stats["authors"] += _apply_authors(cur, paper_id, rec)
                                if (rec.get("externalIds") or {}).get("ArXiv"):
                                    stats["arxiv_filled"] += 1
                        found += len(matched)
                except RuntimeError as e:
                    log.warning("%s %d (venue=%r) 중단: %s", conf, year, alias, e)
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        # search 엔드포인트가 통째로 막힌 상태(실측: 429/500이 몇 분씩
                        # 이어짐). 남은 조합을 계속 두드려도 재시도 시간만 태운다.
                        log.warning("search 엔드포인트가 연속 %d회 실패 — by-venue 중단. "
                                    "나중에 다시 실행하면 이어서 채운다.",
                                    consecutive_failures)
                        log.info("by-venue 결과(중단) %s", stats)
                        return stats
                else:
                    consecutive_failures = 0
                stats["found"] += found
                log.info("%s %d (venue=%r): %d편 매칭", conf, year, alias, found)
                if not titles:
                    break
    log.info("by-venue 결과 %s", stats)
    return stats


# -------------------------------------------------------------- 3. citations

def enrich_citations(client: S2Client) -> dict:
    """코퍼스 내부 인용 엣지만 적재. 외부 논문은 papers에 없어 저장하지 않는다."""
    with cursor() as cur:
        cur.execute("SELECT id, s2_paper_id FROM papers WHERE s2_paper_id IS NOT NULL")
        rows = cur.fetchall()
    if not rows:
        log.info("citations: s2_paper_id가 없다 — 먼저 by-arxiv/by-venue 실행")
        return {"papers": 0, "edges": 0}

    s2_to_paper = {s2: pid for pid, s2 in rows}
    log.info("citations: %d편의 references 조회", len(rows))

    client_batch = client.paper_batch
    edges = 0
    ids = [s2 for _pid, s2 in rows]
    for i in range(0, len(ids), REF_CHUNK):
        chunk = ids[i:i + REF_CHUNK]
        recs = client_batch(chunk, REF_FIELDS)
        pairs = []
        for s2_id, rec in zip(chunk, recs):
            if not rec:
                continue
            citing = s2_to_paper.get(rec.get("paperId") or s2_id)
            if citing is None:
                continue
            for ref in rec.get("references") or []:
                cited = s2_to_paper.get(ref.get("paperId"))
                if cited is not None and cited != citing:
                    pairs.append((citing, cited))
        if pairs:
            with cursor(commit=True) as cur:
                cur.executemany(
                    "INSERT INTO citations (citing_paper_id, cited_paper_id) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING", pairs)
            edges += len(pairs)
        log.info("citations %d/%d (엣지 누적 %d)", min(i + REF_CHUNK, len(ids)),
                 len(ids), edges)
    stats = {"papers": len(rows), "edges": edges}
    log.info("citations 결과 %s", stats)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="Semantic Scholar 보강")
    ap.add_argument("--step", choices=["by-arxiv", "by-venue", "citations", "all"],
                    default="all")
    ap.add_argument("--citations", action="store_true",
                    help="--step citations 와 동일")
    ap.add_argument("--limit", type=int, default=None, help="by-arxiv 시험용 상한")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    step = "citations" if args.citations else args.step
    client = S2Client()
    if step in ("by-arxiv", "all"):
        enrich_by_arxiv(client, args.limit)
    if step in ("by-venue", "all"):
        enrich_by_venue(client)
    if step == "citations":
        enrich_citations(client)


if __name__ == "__main__":
    main()
