from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.onboarding import OnboardingProfile
from app.schemas.common import ApiResponse
from app.schemas.onboarding import OnboardingCreate, OnboardingResponse

# 도메인: onboarding (회원가입 전 익명 상태에서 저장하는 온보딩 답변)
router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


@router.post("", response_model=ApiResponse[OnboardingResponse], status_code=status.HTTP_201_CREATED)
def create_onboarding(payload: OnboardingCreate, db: Session = Depends(get_db)):
    """인증 불필요 — 회원가입 전 익명 상태에서 호출된다.

    이 시점엔 user_id가 없다. 나중에 회원가입 요청(SignupRequest.onboarding_id)이
    이 onboarding_id를 실어 보내면 그때 연결된다 (app/routers/auth.py signup).
    """
    profile = OnboardingProfile(
        user_type=payload.user_type,
        experience=payload.experience,
        purposes=payload.purposes,
        fields=payload.fields,
        stage=payload.stage,
        venue=payload.venue,
        result_order=payload.result_order,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return ApiResponse[OnboardingResponse](data=OnboardingResponse.model_validate(profile))
