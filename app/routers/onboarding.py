from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
from app.database import get_db
from app.models.onboarding import OnboardingProfile
from app.schemas.common import ApiResponse
from app.schemas.onboarding import OnboardingCreate, OnboardingResponse

# 도메인: onboarding (회원가입 전 익명 상태에서 저장하는 온보딩 답변)
router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


@router.post("", response_model=ApiResponse[OnboardingResponse], status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
def create_onboarding(request: Request, payload: OnboardingCreate, db: Session = Depends(get_db)):
    """인증 불필요 — 회원가입 전 익명 상태에서 호출된다.

    이 시점엔 user_id가 없다. 나중에 회원가입 요청(SignupRequest.onboarding_id)이
    이 onboarding_id를 실어 보내면 그때 연결된다 (app/routers/auth.py signup).
    """
    profile = OnboardingProfile(
        user_type=payload.user_type,
        experience=payload.experience,
        fields=payload.fields,
        similarity_focus=payload.similarity_focus,
        recency_bias=payload.recency_bias,
        venue=payload.venue,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return ApiResponse[OnboardingResponse](data=OnboardingResponse.model_validate(profile))
