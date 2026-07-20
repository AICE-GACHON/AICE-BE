from fastapi import APIRouter

# 도메인: feedback (핵심 기능 - 유사 논문 검색 + 예상 리뷰/수정 제안)
# 실제 API 명세서를 확정하면 prefix가 "/api/submissions/{submission_id}/matches",
# ".../predictions"처럼 submission 도메인 하위 경로로 바뀔 가능성이 높습니다.
# 지금은 submission.router와 경로가 겹치지 않도록 임시로 별도 prefix를 씁니다.
router = APIRouter(prefix="/api/feedback", tags=["feedback"])


@router.get("/ping")
def ping():
    """
    feedback 라우터가 정상 연결됐는지 확인하는 임시 테스트 엔드포인트.
    실제 API(유사 논문 매칭 요청, 예상 리뷰/수정 제안 조회 등) 구현하면서
    이 함수는 지우면 됩니다.
    """
    return {"domain": "feedback", "status": "ok"}
