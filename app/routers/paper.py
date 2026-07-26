from fastapi import APIRouter, HTTPException, Query, status

from app.schemas.common import ApiResponse
from app.schemas.paper import PaperListResponse, PaperResponse, RevisionsResponse
from paper_assistant import get_paper_detail, get_paper_revisions
from paper_assistant import list_papers as _list_papers

# 도메인: paper (OpenReview에서 수집한 기존 논문 코퍼스 조회)
router = APIRouter(prefix="/api/papers", tags=["paper"])


@router.get("", response_model=ApiResponse[PaperListResponse])
def list_papers(
    venue: str | None = None,
    year: int | None = None,
    field: str | None = Query(default=None, description="papers.primary_area"),
    q: str | None = Query(default=None, description="제목·초록 전문검색"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """코퍼스 논문 목록. venue/year/field/q로 좁힐 수 있다 (전부 선택)."""
    result = _list_papers(venue=venue, year=year, field=field, q=q, limit=limit, offset=offset)
    return ApiResponse[PaperListResponse](data=result)


@router.get("/{paper_id}", response_model=ApiResponse[PaperResponse])
def get_paper(paper_id: int):
    """유사 논문 1편의 상세 — 초록·저자·메타리뷰·리뷰 전문·지적 항목.

    코퍼스 테이블은 AI 파트가 소유하므로 SQLAlchemy가 아니라 paper_assistant를 통해
    조회합니다. 임베딩 모델을 쓰지 않는 순수 조회라 빠릅니다.

    ⚠️ paper_id는 UUID가 아니라 BIGINT입니다. 분석 결과의
    similar_papers[].paper_id를 그대로 넘기면 됩니다.
    """
    detail = get_paper_detail(paper_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="논문을 찾을 수 없습니다.")
    return ApiResponse[PaperResponse](data=detail)


@router.get("/{paper_id}/revisions", response_model=ApiResponse[RevisionsResponse])
def get_revisions(paper_id: int):
    """저자가 리뷰를 받고 무엇을 고쳤는지 — 제목·초록·PDF 변경 이력.

    ⚠️ 이 엔드포인트만 **외부 네트워크(OpenReview API)를 탑니다.** papers 테이블에는
    최신 버전만 저장되기 때문입니다. 다른 조회보다 느리고 실패할 수 있으니, 프론트는
    사용자가 '수정 이력'을 명시적으로 눌렀을 때만 호출해야 합니다. 논문 상세와 함께
    미리 불러오지 마세요.

    ⚠️ supported=false면 **수정이 없었다는 뜻이 아니라 볼 수 없다는 뜻**입니다
    (2023년 이전 학회는 저자 수정 이력을 공개하지 않습니다). message에 사용자에게
    그대로 보여줄 수 있는 안내 문구가 들어 있으니, 빈 목록으로 처리하지 말고
    그 문구를 노출하세요.
    """
    revisions = get_paper_revisions(paper_id)
    if revisions is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="논문을 찾을 수 없습니다.")
    return ApiResponse[RevisionsResponse](data=revisions)
