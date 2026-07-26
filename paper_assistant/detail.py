"""논문 상세 조회 — 유사 논문을 펼쳤을 때 보여줄 원문·리뷰 전문.

analyze()와 분리된 두 번째 공개 API다. Report에 합치지 않는 이유는 §schemas
PaperDetail 주석 참고 — 이웃 20편의 리뷰 전문은 대부분 열람되지 않는다.

LLM도 임베딩도 쓰지 않는 순수 조회라 응답이 빠르다.
"""
import logging

from paper_assistant.db.connection import cursor
from paper_assistant.schemas import (
    PaperDetail, PaperListResponse, PaperSummary, ReviewDetail, ReviewPointDetail,
)

log = logging.getLogger(__name__)

# 리뷰 정렬: 점수 높은 순. NULL(점수 미기재)은 뒤로.
_REVIEWS_SQL = """
    SELECT rating, rating_raw, confidence, summary, strengths, weaknesses,
           questions, needs_llm_split
    FROM reviews WHERE paper_id = %s
    ORDER BY rating DESC NULLS LAST, id
"""

# aspect='other'는 파이프라인이 패턴 집계에서 제외하는 버킷이다
# (clustering.aggregate_by_aspect의 include_other=False). 분류된 항목을 먼저 보내
# 프론트가 'other'를 접어둘 수 있게 한다.
_POINTS_SQL = """
    SELECT rp.aspect, rp.sentiment, rp.text, r.needs_llm_split
    FROM review_points rp JOIN reviews r ON r.id = rp.review_id
    WHERE rp.paper_id = %s
    ORDER BY CASE rp.sentiment WHEN 'weakness' THEN 0 WHEN 'strength' THEN 1
                               ELSE 2 END,
             (rp.aspect = 'other'), rp.aspect, rp.id
"""

_AUTHORS_SQL = """
    SELECT a.name FROM paper_authors pa JOIN authors a ON a.id = pa.author_id
    WHERE pa.paper_id = %s AND a.name IS NOT NULL
    ORDER BY pa.position NULLS LAST
"""


def get_paper_detail(paper_id: int) -> PaperDetail | None:
    """paper_id로 상세를 조회한다. 없으면 None."""
    with cursor() as cur:
        cur.execute(
            """
            SELECT id, openreview_id, forum_id, arxiv_id, title, abstract,
                   keywords, primary_area, venue, year, decision, meta_review
            FROM papers WHERE id = %s
            """,
            (paper_id,))
        row = cur.fetchone()
        if row is None:
            return None

        (pid, or_id, forum_id, arxiv_id, title, abstract, keywords,
         primary_area, venue, year, decision, meta_review) = row

        cur.execute(_AUTHORS_SQL, (pid,))
        authors = [r[0] for r in cur.fetchall()]

        cur.execute(_REVIEWS_SQL, (pid,))
        reviews = [
            ReviewDetail(
                rating=float(r[0]) if r[0] is not None else None,
                rating_raw=r[1],
                confidence=float(r[2]) if r[2] is not None else None,
                summary=r[3], strengths=r[4], weaknesses=r[5], questions=r[6],
                is_unsplit=bool(r[7]),
            )
            for r in cur.fetchall()
        ]

        cur.execute(_POINTS_SQL, (pid,))
        points = [ReviewPointDetail(aspect=r[0], sentiment=r[1], text=r[2],
                                    from_unsplit_review=bool(r[3]))
                  for r in cur.fetchall()]

    # 포럼은 forum_id 기준이지만 미수집 논문이 있어 openreview_id로 폴백한다.
    forum = forum_id or or_id
    return PaperDetail(
        paper_id=pid, openreview_id=or_id, title=title, abstract=abstract,
        authors=authors, keywords=list(keywords or []), primary_area=primary_area,
        venue=venue, year=year, decision=decision, meta_review=meta_review,
        openreview_url=f"https://openreview.net/forum?id={forum}",
        pdf_url=f"https://openreview.net/pdf?id={or_id}",
        arxiv_url=f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None,
        reviews=reviews, review_points=points,
    )


def list_papers(
    venue: str | None = None,
    year: int | None = None,
    field: str | None = None,
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> PaperListResponse:
    """논문 목록 조회. venue/year/field(primary_area)로 좁히고 q로 제목·초록을
    전문검색한다(papers.tsv, get_paper_detail과 동일하게 LLM·임베딩 없는 순수 조회).
    """
    where: list[str] = []
    params: list = []
    if venue:
        where.append("venue = %s")
        params.append(venue)
    if year is not None:
        where.append("year = %s")
        params.append(year)
    if field:
        where.append("primary_area = %s")
        params.append(field)
    if q:
        where.append("tsv @@ plainto_tsquery('english', %s)")
        params.append(q)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    with cursor() as cur:
        cur.execute(f"SELECT count(*) FROM papers {clause}", params)
        total = cur.fetchone()[0]

        cur.execute(
            f"""
            SELECT id, openreview_id, title, venue, year, decision, primary_area
            FROM papers {clause}
            ORDER BY year DESC, id
            LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        )
        items = [
            PaperSummary(
                paper_id=r[0], openreview_id=r[1], title=r[2], venue=r[3],
                year=r[4], decision=r[5], primary_area=r[6],
            )
            for r in cur.fetchall()
        ]

    return PaperListResponse(total=total, items=items)
