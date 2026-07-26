"""정규화된 논문/리뷰를 Postgres에 적재한다.

수집을 여러 번 나눠 돌려도 안전하도록 전부 upsert(ON CONFLICT)로 작성.
openreview_id가 자연 키이므로 재실행 시 갱신만 일어난다.
"""
import logging

from psycopg.types.json import Json

from paper_assistant.db.connection import cursor
from paper_assistant.ingest.normalize import NormalizedPaper

log = logging.getLogger(__name__)


def upsert_paper(cur, paper: NormalizedPaper, embedding=None) -> int:
    """논문 1편을 upsert하고 paper_id를 반환."""
    cur.execute(
        """
        INSERT INTO papers (openreview_id, forum_id, title, abstract, keywords,
                            primary_area, venue, year, decision, meta_review,
                            embedding)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (openreview_id) DO UPDATE SET
            forum_id     = EXCLUDED.forum_id,
            title        = EXCLUDED.title,
            abstract     = EXCLUDED.abstract,
            keywords     = EXCLUDED.keywords,
            primary_area = EXCLUDED.primary_area,
            venue        = EXCLUDED.venue,
            year         = EXCLUDED.year,
            decision     = EXCLUDED.decision,
            meta_review  = EXCLUDED.meta_review,
            -- 임베딩은 새로 계산했을 때만 덮어쓴다 (재수집 시 불필요한 재계산 방지)
            embedding    = COALESCE(EXCLUDED.embedding, papers.embedding)
        RETURNING id
        """,
        (paper.openreview_id, paper.forum_id, paper.title, paper.abstract,
         paper.keywords, paper.primary_area, paper.venue, paper.year,
         paper.decision, paper.meta_review, embedding),
    )
    return cur.fetchone()[0]


def upsert_authors(cur, paper_id: int, paper: NormalizedPaper) -> None:
    """저자를 upsert하고 논문과 연결. 재투고 매칭에 저자 정보가 필요하다."""
    if not paper.author_ids:
        return
    names = paper.authors or [""] * len(paper.author_ids)
    for position, or_id in enumerate(paper.author_ids):
        name = names[position] if position < len(names) else ""
        cur.execute(
            """
            INSERT INTO authors (openreview_id, name) VALUES (%s, %s)
            ON CONFLICT (openreview_id) DO UPDATE SET
                name = COALESCE(NULLIF(EXCLUDED.name, ''), authors.name)
            RETURNING id
            """,
            (or_id, name),
        )
        author_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO paper_authors (paper_id, author_id, position)
            VALUES (%s, %s, %s)
            ON CONFLICT (paper_id, author_id) DO UPDATE SET
                position = EXCLUDED.position
            """,
            (paper_id, author_id, position),
        )


def upsert_reviews(cur, paper_id: int, paper: NormalizedPaper) -> int:
    for r in paper.reviews:
        if not r.openreview_id:
            continue
        cur.execute(
            """
            INSERT INTO reviews (paper_id, openreview_id, rating, rating_raw,
                                 confidence, summary, strengths, weaknesses,
                                 questions, needs_llm_split)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (openreview_id) DO UPDATE SET
                rating          = EXCLUDED.rating,
                rating_raw      = EXCLUDED.rating_raw,
                confidence      = EXCLUDED.confidence,
                summary         = EXCLUDED.summary,
                strengths       = EXCLUDED.strengths,
                weaknesses      = EXCLUDED.weaknesses,
                questions       = EXCLUDED.questions,
                needs_llm_split = EXCLUDED.needs_llm_split
            """,
            (paper_id, r.openreview_id, r.rating, r.rating_raw, r.confidence,
             r.summary, r.strengths, r.weaknesses, r.questions, r.needs_llm_split),
        )
    return len(paper.reviews)


def load_review_points(review_openreview_id: str, points, embeddings=None) -> int:
    """한 리뷰의 지적 항목을 적재. 재실행 시 기존 항목을 지우고 새로 넣는다.

    points: ExtractedPoint 리스트
    embeddings: 각 point의 벡터 (None이면 임베딩 없이 저장)
    """
    if not points:
        return 0
    with cursor(commit=True) as cur:
        cur.execute(
            "SELECT id, paper_id FROM reviews WHERE openreview_id = %s",
            (review_openreview_id,))
        row = cur.fetchone()
        if not row:
            return 0
        review_id, paper_id = row

        # 멱등성: 이 리뷰의 기존 지적 항목을 먼저 삭제
        cur.execute("DELETE FROM review_points WHERE review_id = %s", (review_id,))

        for i, p in enumerate(points):
            emb = embeddings[i] if embeddings is not None else None
            cur.execute(
                """
                INSERT INTO review_points
                    (review_id, paper_id, aspect, sentiment, text, embedding)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (review_id, paper_id, p.aspect, p.sentiment, p.text, emb),
            )
        cur.execute(
            "UPDATE reviews SET points_extracted = true WHERE id = %s", (review_id,))
    return len(points)


def load_papers(papers: list[NormalizedPaper], embeddings=None) -> tuple[int, int]:
    """논문 + 저자 + 리뷰를 한 트랜잭션으로 적재. (논문 수, 리뷰 수) 반환."""
    n_reviews = 0
    with cursor(commit=True) as cur:
        for i, paper in enumerate(papers):
            emb = embeddings[i] if embeddings is not None else None
            paper_id = upsert_paper(cur, paper, emb)
            upsert_authors(cur, paper_id, paper)
            n_reviews += upsert_reviews(cur, paper_id, paper)
    return len(papers), n_reviews


# --- 수집 체크포인트 ---

def set_status(venue: str, stage: str, status: str,
               processed: int = 0, total: int | None = None,
               error: str | None = None) -> None:
    with cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO ingest_status (venue, stage, status, processed, total, error)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (venue, stage) DO UPDATE SET
                status = EXCLUDED.status, processed = EXCLUDED.processed,
                total = EXCLUDED.total, error = EXCLUDED.error,
                updated_at = now()
            """,
            (venue, stage, status, processed, total, error),
        )


def get_status(venue: str, stage: str) -> dict | None:
    with cursor() as cur:
        cur.execute(
            "SELECT status, processed, total, error FROM ingest_status "
            "WHERE venue = %s AND stage = %s", (venue, stage))
        row = cur.fetchone()
    if not row:
        return None
    return dict(zip(("status", "processed", "total", "error"), row))


def is_done(venue: str, stage: str) -> bool:
    st = get_status(venue, stage)
    return bool(st and st["status"] == "done")
