"""공유 토큰으로 분석 결과를 읽는 **유일한 비인증 경로**.

/api/submissions 하위가 아니라 별도 라우터인 이유는 경로 모양 때문이 아니라
**인증 경계가 다르기 때문**이다. submissions 라우터의 엔드포인트는 전부
get_current_user를 통과하는데, 여기 하나만 예외로 끼워 넣으면 나중에 그 라우터에
"전부 로그인 필요"를 전제한 의존성을 붙이는 순간 이 엔드포인트가 조용히 닫히거나,
반대로 인증 없는 구멍이 목록 한가운데 숨는다. 파일을 나눠 두면 열려 있다는 사실이
파일 이름과 이 주석에 남는다.

여기서 나가는 응답은 SharedAnalysisResponse 하나뿐이고, 무엇을 빼는지는 그
스키마의 docstring이 소유한다.
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
from app.database import get_db
from app.schemas.common import ApiResponse
from app.schemas.share import SharedAnalysisResponse
from app.services import shares as share_service

router = APIRouter(prefix="/api/shared", tags=["shared"])

# **IP 기준** 상한이다 (계정 기준이 아니라). 이 경로에는 계정이 없으므로 IP가
# 유일하게 셀 수 있는 단위다 — corpus.py의 `/story`와 같은 이유다.
#
# 여기서 막는 것은 비용이 아니라 **대입 시도가 DB로 내려가는 것**이다. 토큰은
# 256비트라 훑는 것 자체가 불가능하므로, 상한이 방어의 주력이 아니다.
#
# ⚠️ 그래서 값이 넉넉하다. **이 경로는 다른 어떤 엔드포인트보다 "여러 사람이 한 IP
# 뒤에서" 부르기 쉽다** — 널리 뿌리라고 만든 링크이기 때문이다. 강의실이나 회사
# NAT 뒤에서 30명이 링크를 열고 각자 두어 번 새로고침하면 수십 건이 한 IP로 뭉친다.
# 조이면 막는 것은 공격자가 아니라 그 사람들이다(rate_limit.py의 NAT 경고 참고).
# 300/hour는 그런 정상 사용을 통과시키면서, 자동화된 반복 조회는 그대로 끊는다.
_SHARED_VIEW_LIMIT = "300/hour"


@router.get("/{token}", response_model=ApiResponse[SharedAnalysisResponse])
@limiter.limit(_SHARED_VIEW_LIMIT)
def get_shared_analysis(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
):
    """공유 링크로 분석 결과를 연다. **로그인이 필요 없다.**

    없는 토큰·폐기된 토큰·결과가 없는 초안이 전부 404입니다 (사유를 구분해 주면
    대입 시도에 힌트가 됩니다 — services/shares.py의 resolve 참고).

    소유자가 같은 초안을 다시 분석하면 이 링크의 내용도 새 결과로 바뀝니다.
    """
    submission, prediction = share_service.resolve(db, token)
    return ApiResponse[SharedAnalysisResponse](data=SharedAnalysisResponse(
        title=submission.title,
        abstract=submission.abstract,
        field=submission.field,
        report=prediction.report,
    ))
