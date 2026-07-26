"""arXiv 매칭 — 로컬 arXiv 캐시로 papers.arxiv_id를 채운다 (설계 §5, §6).

매칭 규칙 (오탐이 재투고 링크를 오염시키므로 보수적으로):
  1. 제목 정규화(소문자+영숫자) 후 완전 일치만 후보로 본다. 부분 일치는 쓰지 않는다.
  2. 후보가 여럿이면 저자 성(姓) 겹침이 가장 큰 것을 택하고, 동점이면 포기한다.
  3. 저자 정보가 양쪽에 다 있으면 성이 최소 1개는 겹쳐야 한다. 동명 논문
     ("Learning to Optimize" 류)이 실제로 존재하기 때문이다.
  4. 정규화 제목이 짧으면(< MIN_TITLE_LEN) 일반 명사구 충돌 위험이 커 건너뛴다.
  5. arXiv 등록일이 투고 연도보다 늦은 경우(> 1년)는 다른 논문으로 본다.

사용법:
    python -m paper_assistant.ingest.arxiv_matcher            # 실제 반영
    python -m paper_assistant.ingest.arxiv_matcher --dry-run  # 통계만
"""
import argparse
import logging
import re

from paper_assistant.db.connection import cursor
from paper_assistant.ingest.arxiv_client import iter_cached
from paper_assistant.ingest.submission_linker import normalize_title

MIN_TITLE_LEN = 25          # 정규화 제목 최소 길이
MAX_YEARS_AFTER = 1         # arXiv 등록이 투고 연도보다 이만큼 늦으면 제외

log = logging.getLogger("arxiv-match")


def surname(name: str) -> str:
    """'Alice B. Kim' → 'kim'. arXiv keyname과 맞추기 위한 마지막 토큰."""
    tokens = re.findall(r"[A-Za-z][A-Za-z'\-]+", name or "")
    return tokens[-1].lower() if tokens else ""


def build_index(records=None) -> dict[str, list[dict]]:
    """정규화 제목 → arXiv 레코드 목록. 같은 arXiv id는 최신 것으로 덮어쓴다."""
    by_id: dict[str, dict] = {}
    for rec in (records if records is not None else iter_cached()):
        by_id[rec["id"]] = rec

    index: dict[str, list[dict]] = {}
    for rec in by_id.values():
        key = normalize_title(rec["title"])
        if len(key) < MIN_TITLE_LEN:
            continue
        index.setdefault(key, []).append(rec)
    return index


def _created_year(rec: dict) -> int | None:
    created = rec.get("created") or ""
    return int(created[:4]) if created[:4].isdigit() else None


def choose(paper: dict, candidates: list[dict]) -> dict | None:
    """후보 중 하나를 고른다. 확정할 수 없으면 None (규칙 2~5)."""
    paper_surnames = paper["surnames"]
    viable = []
    for rec in candidates:
        year = _created_year(rec)
        if year is not None and year > paper["year"] + MAX_YEARS_AFTER:
            continue
        rec_surnames = {surname(k) for k in rec["keynames"]} - {""}
        overlap = len(paper_surnames & rec_surnames)
        if paper_surnames and rec_surnames and overlap == 0:
            continue
        viable.append((overlap, rec))
    if not viable:
        return None
    viable.sort(key=lambda t: -t[0])
    if len(viable) > 1 and viable[0][0] == viable[1][0]:
        return None          # 동점 — 어느 쪽인지 확정 불가
    return viable[0][1]


def _fetch_papers(only_missing: bool) -> list[dict]:
    where = "WHERE p.arxiv_id IS NULL" if only_missing else ""
    with cursor() as cur:
        cur.execute(f"""
            SELECT p.id, p.title, p.year,
                   coalesce(array_agg(a.name) FILTER (WHERE a.name IS NOT NULL), '{{}}')
            FROM papers p
            LEFT JOIN paper_authors pa ON pa.paper_id = p.id
            LEFT JOIN authors a ON a.id = pa.author_id
            {where}
            GROUP BY p.id
        """)
        return [{"id": r[0], "title": r[1], "year": r[2],
                 "surnames": {surname(n) for n in r[3]} - {""}}
                for r in cur.fetchall()]


def match_papers(papers: list[dict], index: dict[str, list[dict]]) -> tuple[list[tuple], dict]:
    """(paper_id, arxiv_id) 목록과 통계를 돌려준다."""
    updates: list[tuple[str, int]] = []
    stats = {"papers": len(papers), "title_hit": 0, "matched": 0,
             "short_title": 0, "ambiguous": 0}
    for p in papers:
        key = normalize_title(p["title"])
        if len(key) < MIN_TITLE_LEN:
            stats["short_title"] += 1
            continue
        candidates = index.get(key)
        if not candidates:
            continue
        stats["title_hit"] += 1
        rec = choose(p, candidates)
        if rec is None:
            stats["ambiguous"] += 1
            continue
        updates.append((rec["id"], p["id"]))
        stats["matched"] += 1
    return updates, stats


def run_matching(only_missing: bool = True, dry_run: bool = False) -> dict:
    log.info("arXiv 캐시 인덱싱…")
    index = build_index()
    log.info("고유 제목 %d개", len(index))

    papers = _fetch_papers(only_missing)
    updates, stats = match_papers(papers, index)

    if not dry_run and updates:
        with cursor(commit=True) as cur:
            cur.executemany("UPDATE papers SET arxiv_id = %s WHERE id = %s", updates)
    log.info("매칭 결과 %s%s", stats, " (dry-run)" if dry_run else "")
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="arXiv id 매칭")
    ap.add_argument("--dry-run", action="store_true", help="DB 수정 없이 통계만")
    ap.add_argument("--all", action="store_true",
                    help="arxiv_id가 이미 있는 논문도 다시 매칭")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_matching(only_missing=not args.all, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
