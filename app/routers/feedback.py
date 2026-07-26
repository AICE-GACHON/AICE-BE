import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.database import get_db
from app.models.feedback import ReviewPrediction
from app.models.submission import SimilarPaperMatch, Submission
from app.models.user import User
from app.schemas.common import ApiResponse
from app.schemas.feedback import AnalysisResponse, AnalysisStartResponse
from app.schemas.submission import SimilarPaperMatchResponse
from app.services.analysis import run_analysis

# 도메인: feedback (핵심 기능 - 유사 논문 검색 + 예상 리뷰 분석)
#
# 경로는 submission 하위로 둡니다 — 분석은 항상 "특정 초안 하나에 대한" 작업이라
# submission_id가 경로에 있는 편이 소유권 검증과 폴링 모두에 자연스럽습니다.
router = APIRouter(prefix="/api/submissions", tags=["feedback"])

# 아직 끝나지 않은 분석 상태
_IN_PROGRESS = ("pending", "running")


def _get_owned_submission(db: Session, submission_id: uuid.UUID, user: User) -> Submission:
    """내 초안일 때만 돌려준다. 남의 초안은 존재 여부도 알리지 않도록 404로 통일."""
    submission = db.get(Submission, submission_id)
    if submission is None or submission.user_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="논문 초안을 찾을 수 없습니다.")
    return submission


def _latest_prediction(db: Session, submission_id: uuid.UUID) -> ReviewPrediction | None:
    return (
        db.query(ReviewPrediction)
        .filter(ReviewPrediction.submission_id == submission_id)
        .order_by(ReviewPrediction.created_at.desc())
        .first()
    )


@router.post(
    "/{submission_id}/analysis",
    response_model=ApiResponse[AnalysisStartResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
def start_analysis(
    submission_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """분석을 시작한다. 결과를 기다리지 않고 202로 즉시 돌아온다.

    analyze() 한 번에 임베딩 로드 + 검색 + 집계가 들어가 수 초~수십 초 걸리기 때문에
    동기로 응답하지 않습니다. 프론트는 202를 받은 뒤 GET으로 폴링하세요.

    이미 진행 중인 분석이 있으면 새로 만들지 않고 그것을 돌려줍니다 (중복 실행 방지).
    """
    _get_owned_submission(db, submission_id, current_user)

    existing = _latest_prediction(db, submission_id)
    if existing is not None and existing.status in _IN_PROGRESS:
        return ApiResponse[AnalysisStartResponse](
            data=AnalysisStartResponse.model_validate(existing)
        )

    prediction = ReviewPrediction(submission_id=submission_id, status="pending")
    db.add(prediction)
    db.commit()
    db.refresh(prediction)

    # 응답을 보낸 뒤 실행된다. 요청 세션은 그때 닫혀 있으므로 id만 넘긴다.
    background_tasks.add_task(run_analysis, prediction.prediction_id)

    return ApiResponse[AnalysisStartResponse](
        data=AnalysisStartResponse.model_validate(prediction)
    )


@router.get("/{submission_id}/analysis", response_model=ApiResponse[AnalysisResponse])
def get_analysis(
    submission_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """가장 최근 분석의 상태와 결과를 돌려준다 (폴링 대상).

    status가 done이면 report에 분석 결과 전체가, failed면 error에 사유가 들어갑니다.
    report를 화면에 옮길 때 주의할 점은 AI_파트_팀_공유.md §4를 꼭 읽어주세요 —
    특히 confidence.level이 weak이면 경고 없이 결과만 보여주면 안 됩니다.
    """
    _get_owned_submission(db, submission_id, current_user)

    prediction = _latest_prediction(db, submission_id)
    if prediction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="아직 분석을 시작하지 않았습니다.",
        )

    matches = (
        db.query(SimilarPaperMatch)
        .filter(SimilarPaperMatch.prediction_id == prediction.prediction_id)
        .order_by(SimilarPaperMatch.rank)
        .all()
    )

    data = AnalysisResponse.model_validate(prediction)
    data.matches = [SimilarPaperMatchResponse.model_validate(m) for m in matches]
    return ApiResponse[AnalysisResponse](data=data)
