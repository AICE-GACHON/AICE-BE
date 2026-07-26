"""파일럿 적재 + 하이브리드 검색 검증 (end-to-end).

ICLR 2024 논문 N편을 수집 → 임베딩 → Postgres 적재 → 검색까지 실제로 돌려본다.
DB 컨테이너가 떠 있어야 한다: docker compose up -d
"""
import logging
import sys
import time

from paper_assistant.db import load
from paper_assistant.db.connection import cursor
from paper_assistant.embedding.specter2 import Specter2Embedder
from paper_assistant.ingest.normalize import normalize_paper
from paper_assistant.ingest.openreview_client import VENUE_REGISTRY, get_client
from paper_assistant.retrieval.hybrid_search import hybrid_search

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

VENUE = "ICLR 2024"


def main(n_papers: int = 200, with_reviews: bool = False):
    base, invitation = VENUE_REGISTRY[VENUE]
    client = get_client(base)
    year = int(VENUE.split()[-1])

    # 1) 수집 + 정규화
    log.info("논문 %d편 수집 중...", n_papers)
    load.set_status(VENUE, "fetch", "running")
    papers = []
    for note in client.iter_notes(invitation=invitation):
        replies = client.get_forum_replies(note["forum"]) if with_reviews else []
        p = normalize_paper(note, replies, VENUE, year)
        if p.title and p.abstract:
            papers.append(p)
        if len(papers) >= n_papers:
            break
    load.set_status(VENUE, "fetch", "done", processed=len(papers), total=len(papers))
    log.info("수집 완료: %d편", len(papers))

    # 2) 임베딩
    embedder = Specter2Embedder()
    start = time.perf_counter()
    vecs = embedder.encode([(p.title, p.abstract) for p in papers], batch_size=32)
    log.info("임베딩 완료: %d편 %.1f초", len(papers), time.perf_counter() - start)

    # 3) 적재
    start = time.perf_counter()
    n_p, n_r = load.load_papers(papers, [v.numpy() for v in vecs])
    log.info("적재 완료: 논문 %d편 / 리뷰 %d건 (%.1f초)",
             n_p, n_r, time.perf_counter() - start)
    load.set_status(VENUE, "embed", "done", processed=n_p, total=n_p)

    # 4) DB 상태 확인
    with cursor() as cur:
        cur.execute("SELECT count(*), count(embedding), count(tsv) FROM papers")
        total, with_emb, with_tsv = cur.fetchone()
        cur.execute("SELECT count(*) FROM reviews")
        n_reviews = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM authors")
        n_authors = cur.fetchone()[0]
    print(f"\n{'='*70}")
    print(f"DB 상태: 논문 {total}편 (임베딩 {with_emb}, tsvector {with_tsv}) / "
          f"리뷰 {n_reviews}건 / 저자 {n_authors}명")
    print(f"{'='*70}")

    # 5) 하이브리드 검색 검증
    query = papers[0]
    print(f"\n[검색 테스트] 쿼리 논문: {query.title[:70]}")
    print(f"  (이 논문 자신이 1위로 나와야 정상)\n")

    qvec = embedder.encode_one(query.title, query.abstract).numpy()
    start = time.perf_counter()
    results = hybrid_search(qvec, f"{query.title} {query.abstract}", top_k=5)
    elapsed = (time.perf_counter() - start) * 1000

    # 코사인은 진단용으로만 찍는다 — 사용자 대상 출력에는 절대 넣지 않는다 (§20)
    print(f"{'순위':>3} {'RRF':>7} {'벡터':>5} {'FTS':>5} {'매칭':>9} {'cos':>7}  제목")
    print("-" * 84)
    for i, r in enumerate(results, 1):
        vr = r.vector_rank if r.vector_rank else "-"
        fr = r.fts_rank if r.fts_rank else "-"
        cos = f"{r.cosine:.4f}" if r.cosine is not None else "-"
        print(f"{i:>3} {r.rrf_score:>7.4f} {vr:>5} {fr:>5} {r.match_type:>9} "
              f"{cos:>7}  {r.title[:40]}")
    print(f"\n검색 소요: {elapsed:.0f}ms")

    if results and results[0].openreview_id == query.openreview_id:
        print("✅ 자기 자신이 1위 — 검색 파이프라인 정상")
    else:
        print("⚠️  자기 자신이 1위가 아님 — 확인 필요")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    reviews = "--reviews" in sys.argv
    main(n, reviews)
