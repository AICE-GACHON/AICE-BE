"""공개 공유 링크의 생애주기 — 발급·폐기·토큰 조회.

**여기에는 정책만 둔다** (누가 공유할 수 있는지, 폐기된 토큰을 어떻게 취급하는지).
경로·인증·rate limit은 app/routers의 몫이다.

공유는 submission에 붙지 특정 분석 회차에 붙지 않는다. 그래서 소유자가 같은 초안을
다시 분석하면 공유 링크의 내용도 새 결과로 바뀐다 — 링크를 새로 만들 필요가 없고,
반대로 옛 결과를 박제하고 싶다면 그때는 스키마에 prediction_id를 둬야 한다.
"""
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.analysis import ReviewPrediction
from app.models.share import SubmissionShare
from app.models.submission import Submission
from app.services.analysis import latest_done_prediction


def _now() -> datetime:
    return datetime.now(timezone.utc)


def share_url(token: str) -> str:
    """링크에 넣을 절대 주소.

    **서버가 조립한다.** 프론트가 만들 수도 있지만, 그러면 같은 규칙이 두 곳에
    생기고 한쪽만 바뀌었을 때 이미 공유된 링크가 깨진다. 비밀번호 재설정 링크가
    같은 이유로 FRONTEND_BASE_URL을 쓴다(app/core/mail.py).

    ⚠️ 경로(/shared/)는 프론트 라우트와의 약속이다. 프론트가 라우트를 옮기면
    여기도 함께 바꿔야 하고, 그 전에 나간 링크는 되돌릴 수 없다.
    """
    return f"{settings.FRONTEND_BASE_URL.rstrip('/')}/shared/{token}"


def active_share(db: Session, submission_id: uuid.UUID) -> SubmissionShare | None:
    """살아 있는 공유 1건. 부분 유니크 인덱스가 최대 1건을 보장한다."""
    return db.scalars(
        select(SubmissionShare)
        .where(SubmissionShare.submission_id == submission_id,
               SubmissionShare.revoked_at.is_(None))
    ).first()


def issue(db: Session, submission: Submission) -> SubmissionShare:
    """공유 링크를 확보한다. 이미 있으면 **그대로 돌려준다** (멱등).

    매번 새 토큰을 발급하면 사용자가 공유 버튼을 두 번 눌렀다는 이유로 먼저 보낸
    링크가 죽는다. 프론트도 "현재 공유 URL 1개"만 필요로 한다.

    분석이 끝나지 않았으면 거부한다 — 보여줄 결과가 없는 링크를 만들면 받는 사람은
    빈 화면을 본다. 실패한 분석도 마찬가지다.
    """
    existing = active_share(db, submission.submission_id)
    if existing is not None:
        return existing

    if latest_done_prediction(db, submission.submission_id) is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="분석이 완료된 뒤에 공유할 수 있습니다.")

    share = SubmissionShare(submission_id=submission.submission_id)
    db.add(share)
    try:
        db.commit()
    except IntegrityError:
        # 같은 순간에 들어온 다른 요청이 먼저 만들었다 → 그 행을 돌려준다.
        # 부분 유니크 인덱스가 없으면 여기서 두 개가 생기고, 그러면 폐기 API가
        # 하나만 지워서 **폐기했다고 응답한 링크가 계속 열린다.**
        db.rollback()
        existing = active_share(db, submission.submission_id)
        if existing is None:
            raise
        return existing

    db.refresh(share)
    return share


def revoke(db: Session, submission_id: uuid.UUID) -> bool:
    """공유를 폐기한다. 폐기할 것이 있었으면 True.

    행을 지우지 않고 revoked_at을 찍는다 — 되돌린 사실도 기록이고, 지우면 같은
    토큰이 다시 나올 여지가 생긴다(unique는 지워진 값을 막지 못한다).

    이미 폐기됐거나 애초에 없었어도 예외를 던지지 않는다. 폐기는 멱등해야 한다:
    두 번 눌렀다고 404를 주면 화면은 "실패"로 보이는데 실제 상태는 원하던 대로다.
    """
    share = active_share(db, submission_id)
    if share is None:
        return False
    share.revoked_at = _now()
    db.commit()
    return True


def resolve(db: Session, token: str) -> tuple[Submission, ReviewPrediction]:
    """공개 토큰 → (초안, 완료된 분석). 열 수 없으면 무조건 404.

    ⚠️ **실패 사유를 구분해 내보내지 않는다.** 없는 토큰·폐기된 토큰·아직 분석이
    없는 초안이 전부 같은 404다. 구분해 주면 대입 공격자에게 "이 토큰은 존재하긴
    한다"를 알려주는 셈이고, 폐기했는데 "폐기됨"이라고 답하는 것은 공유를 되돌린
    사람의 의도에도 어긋난다(그 초안이 존재한다는 사실 자체가 새어 나간다).
    """
    share = db.scalars(
        select(SubmissionShare).where(SubmissionShare.token == token)
    ).first()
    if share is None or share.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="공유된 분석 결과를 찾을 수 없습니다.")

    submission = db.get(Submission, share.submission_id)
    prediction = (latest_done_prediction(db, share.submission_id)
                  if submission is not None else None)
    if submission is None or prediction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="공유된 분석 결과를 찾을 수 없습니다.")
    return submission, prediction
