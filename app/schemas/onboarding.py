import uuid
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.schemas.common import ORMBase, TimestampMixin

# 이 엔드포인트는 **인증이 없다** (회원가입 전에 불린다). 상한이 없으면 누구나
# 임의 길이의 문자열과 임의 개수의 배열 원소를 DB에 넣을 수 있다. 두 가지가 걸린다:
#
#  1. 문자열 컬럼은 String(50)이다. 그보다 긴 값은 pydantic을 통과해
#     DB에서 터지고, 전역 핸들러가 500으로 바꾼다 — 사용자 입력이 만든 500은
#     그 자체로 잘못된 신호다(422여야 한다).
#  2. fields/venue는 JSONB라 DB 상한이 없다. 5회/분 제한 안에서도 한 번에 수
#     MB씩 꾸준히 밀어 넣으면 저장소가 는다.
#
# 값 자체는 프론트가 주는 고정 선택지라(onboardingData.js), 아래 상한은 정상
# 입력보다 한참 넉넉하다.
_MAX_SHORT = 50    # user_type, experience, similarity_focus, recency_bias — String(50)
_MAX_ITEMS = 20    # 배열 길이 (fields/venue)

# 배열 원소 하나의 길이 상한. list[str]에 Field(max_length=...)를 걸면 **개수만**
# 제한된다 — 원소 자체는 얼마든지 길 수 있으므로 타입 쪽에서 따로 잠근다.
Item = Annotated[str, StringConstraints(max_length=100)]


class OnboardingCreate(BaseModel):
    user_type: str | None = Field(default=None, max_length=_MAX_SHORT)
    experience: str | None = Field(default=None, max_length=_MAX_SHORT)
    fields: list[Item] = Field(default=[], max_length=_MAX_ITEMS)
    # 둘 다 분석에 쓰인다 (models/onboarding.py 참고). **여기서 열거형으로 잠그지
    # 않는 이유**: 이 엔드포인트가 거절하면 프론트가 선택지를 늘리는 순간 온보딩
    # 저장이 통째로 실패한다. 대신 쓰는 쪽이 화이트리스트로 거르고 모르는 값은
    # "균형있게"로 접는다 (paper_assistant.schemas.SearchPreferences).
    similarity_focus: str | None = Field(default=None, max_length=_MAX_SHORT)
    recency_bias: str | None = Field(default=None, max_length=_MAX_SHORT)
    venue: list[Item] = Field(default=[], max_length=_MAX_ITEMS)


class OnboardingUpdate(BaseModel):
    """마이페이지에서 온보딩 답변을 고칠 때 쓴다 (PATCH /api/user/me/onboarding).

    **보낸 필드만 갱신한다.** OnboardingCreate와 달리 리스트에도 `None` 기본값을
    두는 이유가 여기 있다 — `default=[]`로 두면 "안 보냄"과 "비웠음"이 둘 다
    빈 리스트가 되어, 닉네임 하나 고치려고 PATCH를 보냈을 뿐인데 관심 분야가
    통째로 지워진다. `None`이면 건드리지 않고, `[]`면 의도적으로 비운 것이다.

    상한은 OnboardingCreate와 같은 값을 쓴다. 이쪽은 인증이 필요해서 익명
    엔드포인트만큼 급하진 않지만, **규칙이 갈리면 느슨한 쪽이 우회로가 된다** —
    생성 때 막은 길이를 수정으로 넣을 수 있으면 막은 의미가 없다.
    """
    user_type: str | None = Field(default=None, max_length=_MAX_SHORT)
    experience: str | None = Field(default=None, max_length=_MAX_SHORT)
    fields: list[Item] | None = Field(default=None, max_length=_MAX_ITEMS)
    similarity_focus: str | None = Field(default=None, max_length=_MAX_SHORT)
    recency_bias: str | None = Field(default=None, max_length=_MAX_SHORT)
    venue: list[Item] | None = Field(default=None, max_length=_MAX_ITEMS)


class OnboardingResponse(ORMBase, TimestampMixin):
    onboarding_id: uuid.UUID
    user_type: str | None
    experience: str | None
    fields: list[str]
    similarity_focus: str | None
    recency_bias: str | None
    venue: list[str]
