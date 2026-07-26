"""강/약점 미분리 리뷰의 지적 항목을 다시 추출한다.

기존 추출기는 미분리 리뷰의 본문 전체를 weakness로 라벨링했다. 그 결과
논문당 지적 수가 1.47배(27.9 vs 18.9건) 부풀고 aspect 지적률이 최대 1.39배
왜곡됐다. review_extractor.split_weakness_section이 'Cons'/'Weaknesses' 같은
머리말로 약점 섹션을 되살리고, 되살리지 못한 리뷰는 sentiment='unknown'으로
남겨 집계에서 빠지게 한다.

미분리 리뷰(needs_llm_split)만 건드린다 — 분리 포맷 리뷰의 항목은 그대로 둔다.
실행 후 base rate가 바뀌므로 scripts/build_base_rates.py를 다시 돌릴 것.

    $env:PYTHONPATH="."; python scripts/reextract_unsplit.py [--dry-run]
"""
import argparse
import logging

from paper_assistant.db.connection import cursor
from paper_assistant.ingest.normalize import NormalizedReview
from paper_assistant.ingest.review_extractor import HeuristicExtractor

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("reextract")

BATCH = 2000

SELECT_SQL = """
    SELECT id, openreview_id, summary, strengths, weaknesses, questions
    FROM reviews WHERE needs_llm_split ORDER BY id
"""


def main(dry_run: bool = False) -> None:
    extractor = HeuristicExtractor()

    with cursor() as cur:
        cur.execute(SELECT_SQL)
        rows = cur.fetchall()
    log.info("미분리 리뷰 %s건 재추출 시작%s", f"{len(rows):,}",
             " (dry-run)" if dry_run else "")

    stats = {"recovered": 0, "unknown": 0, "empty": 0}
    pending: list[tuple[int, list]] = []
    n_points = 0

    for i, (review_id, _or_id, summary, strengths, weaknesses, questions) in \
            enumerate(rows, 1):
        review = NormalizedReview(
            openreview_id=_or_id, rating=None, rating_raw="", confidence=None,
            summary=summary or "", strengths=strengths or "",
            weaknesses=weaknesses or "", questions=questions or "",
            needs_llm_split=True)
        points = extractor.extract(review)

        if not points:
            stats["empty"] += 1
        elif any(p.sentiment == "weakness" for p in points):
            stats["recovered"] += 1
        else:
            stats["unknown"] += 1

        pending.append((review_id, points))
        n_points += len(points)

        if not dry_run and len(pending) >= BATCH:
            _flush(pending)
            pending = []
            log.info("  %s / %s 처리", f"{i:,}", f"{len(rows):,}")

    if not dry_run and pending:
        _flush(pending)

    print(f"\n미분리 리뷰 {len(rows):,}건")
    print(f"  약점 섹션 복구  {stats['recovered']:,} "
          f"({100*stats['recovered']/len(rows):.1f}%)")
    print(f"  unknown 처리    {stats['unknown']:,} "
          f"({100*stats['unknown']/len(rows):.1f}%)")
    print(f"  항목 없음       {stats['empty']:,}")
    print(f"  총 지적 항목    {n_points:,}")
    if dry_run:
        print("\n(dry-run — DB를 바꾸지 않았습니다)")
    else:
        print("\n✅ 완료. scripts/build_base_rates.py를 다시 실행하세요.")


def _flush(pending: list[tuple[int, list]]) -> None:
    """배치 단위로 기존 항목을 지우고 새 항목을 넣는다."""
    review_ids = [rid for rid, _ in pending]
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM review_points WHERE review_id = ANY(%s)",
                    (review_ids,))
        params = [
            (rid, p.aspect, p.sentiment, p.text)
            for rid, points in pending for p in points
        ]
        if params:
            cur.executemany(
                """
                INSERT INTO review_points (review_id, paper_id, aspect,
                                           sentiment, text)
                SELECT %s, r.paper_id, %s, %s, %s FROM reviews r WHERE r.id = %s
                """,
                [(rid, aspect, sentiment, text, rid)
                 for rid, aspect, sentiment, text in params])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="DB를 바꾸지 않고 통계만 출력")
    main(**vars(ap.parse_args()))
