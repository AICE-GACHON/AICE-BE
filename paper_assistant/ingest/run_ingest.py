"""전체 수집·적재 파이프라인 (백그라운드 배치).

VENUE_REGISTRY의 10개 venue를 순회하며:
  1. 제출 논문 + forum 리플라이 조회 (이미 적재된 논문은 건너뜀 — 멱등 재개)
  2. 정규화 (venue×연도별 필드 차이 흡수)
  3. SPECTER2로 논문 임베딩
  4. 휴리스틱으로 리뷰 지적항목 추출 ($0, 텍스트만 저장 — 임베딩은 쿼리 시점)
  5. Postgres 적재 (upsert)

재개 가능: 적재가 전부 upsert이고, venue 시작 시 이미 임베딩된 논문 id를
건너뛰므로 중간에 끊겨도 다시 실행하면 이어서 진행된다.

사용법:
    python -m paper_assistant.ingest.run_ingest              # 전체
    python -m paper_assistant.ingest.run_ingest "ICLR 2024"  # 특정 venue만
"""
import logging
import sys
import time

from paper_assistant.db import load
from paper_assistant.db.connection import cursor
from paper_assistant.embedding.specter2 import Specter2Embedder
from paper_assistant.ingest.normalize import normalize_paper
from paper_assistant.ingest.openreview_client import VENUE_REGISTRY, get_client
from paper_assistant.ingest.review_extractor import HeuristicExtractor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ingest")

FLUSH_EVERY = 200        # 이만큼 모이면 임베딩+적재
FORUM_SLEEP = 0.2        # forum 조회 간 대기 (rate limit 예방)


def _existing_ids(venue: str) -> set[str]:
    """이미 임베딩까지 끝난 논문 id (재개 시 건너뛸 대상)."""
    with cursor() as cur:
        cur.execute(
            "SELECT openreview_id FROM papers "
            "WHERE venue = %s AND embedding IS NOT NULL", (venue,))
        return {r[0] for r in cur.fetchall()}


def _flush(buffer, embedder, extractor):
    """버퍼의 논문들을 임베딩·추출·적재하고 (논문 수, 리뷰 수, 지적항목 수) 반환."""
    if not buffer:
        return 0, 0, 0
    vecs = embedder.encode([(p.title, p.abstract) for p in buffer], batch_size=32)
    n_p, n_r = load.load_papers(buffer, [v.numpy() for v in vecs])

    n_points = 0
    for paper in buffer:
        for review in paper.reviews:
            if not review.openreview_id:
                continue
            points = extractor.extract(review)
            # 지적항목은 텍스트만 저장한다 — 임베딩은 쿼리 시점에 계산한다(§13).
            n_points += load.load_review_points(review.openreview_id, points)
    return n_p, n_r, n_points


def ingest_venue(venue: str, embedder, extractor) -> None:
    base, invitation = VENUE_REGISTRY[venue]
    client = get_client(base)
    year = int(venue.split()[-1])

    total = client.count_notes(invitation=invitation)
    done_ids = _existing_ids(venue)
    log.info("=== %s 시작 — 총 %d편 (이미 완료 %d편) ===",
             venue, total, len(done_ids))
    load.set_status(venue, "ingest", "running", processed=len(done_ids), total=total)

    buffer = []
    seen = fetched = n_reviews = n_points = 0
    start = time.perf_counter()

    for note in client.iter_notes(invitation=invitation):
        seen += 1
        oid = note.get("id", "")
        if oid in done_ids:
            continue  # 이미 완료 — forum 조회 자체를 건너뜀

        replies = client.get_forum_replies(note["forum"])
        time.sleep(FORUM_SLEEP)
        paper = normalize_paper(note, replies, venue, year)
        if not paper.title or not paper.abstract:
            continue
        buffer.append(paper)
        fetched += 1

        if len(buffer) >= FLUSH_EVERY:
            _, r, pts = _flush(buffer, embedder, extractor)
            n_reviews += r
            n_points += pts
            processed = len(done_ids) + fetched
            rate = fetched / (time.perf_counter() - start)
            eta_min = (total - processed) / rate / 60 if rate > 0 else 0
            log.info("%s: %d/%d 적재 (리뷰 %d, 지적항목 %d) — %.1f편/s, ETA %.0f분",
                     venue, processed, total, n_reviews, n_points, rate, eta_min)
            load.set_status(venue, "ingest", "running", processed=processed, total=total)
            buffer = []

    _, r, pts = _flush(buffer, embedder, extractor)
    n_reviews += r
    n_points += pts
    load.set_status(venue, "ingest", "done",
                    processed=len(done_ids) + fetched, total=total)
    log.info("=== %s 완료 — 신규 %d편 / 리뷰 %d / 지적항목 %d (%.0f분) ===",
             venue, fetched, n_reviews, n_points, (time.perf_counter() - start) / 60)


def main(venues: list[str]):
    log.info("SPECTER2 로드 중...")
    embedder = Specter2Embedder()
    extractor = HeuristicExtractor()

    grand_start = time.perf_counter()
    for venue in venues:
        if load.is_done(venue, "ingest"):
            log.info("=== %s 이미 완료 — 건너뜀 ===", venue)
            continue
        try:
            ingest_venue(venue, embedder, extractor)
        except Exception as e:
            log.exception("%s 실패: %s", venue, e)
            load.set_status(venue, "ingest", "failed", error=str(e)[:500])

    with cursor() as cur:
        cur.execute("SELECT count(*), count(embedding) FROM papers")
        total, embedded = cur.fetchone()
        cur.execute("SELECT count(*) FROM reviews")
        n_reviews = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM review_points")
        n_points = cur.fetchone()[0]
    log.info("전체 완료 (%.0f분): 논문 %d편(임베딩 %d) / 리뷰 %d / 지적항목 %d",
             (time.perf_counter() - grand_start) / 60,
             total, embedded, n_reviews, n_points)


if __name__ == "__main__":
    requested = sys.argv[1:] or list(VENUE_REGISTRY.keys())
    for v in requested:
        if v not in VENUE_REGISTRY:
            log.error("알 수 없는 venue: %s (가능: %s)", v, list(VENUE_REGISTRY))
            sys.exit(1)
    main(requested)
