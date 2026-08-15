"""약관·개인정보처리방침 원문 조회.

**인증이 없다.** 회원가입 화면이 계정을 만들기 *전에* 보여줘야 하는 문서라서,
토큰을 요구하면 동의를 받아야 할 바로 그 화면에서 못 읽는다. 탈퇴한 사람이
"내가 동의했던 내용"을 다시 확인하는 것도 막지 않는다.

원문은 app/legal/*.md에 있고 프로세스당 한 번만 읽는다 (app/core/legal.py).
"""
from fastapi import APIRouter, HTTPException, Request, status

from app.core.legal import DOCUMENTS, load_document
from app.core.rate_limit import limiter
from app.schemas.common import ApiResponse
from app.schemas.legal import LegalDocumentResponse

router = APIRouter(prefix="/api/legal", tags=["legal"])


@router.get("/{document}", response_model=ApiResponse[LegalDocumentResponse])
# 메모리에서 문자열을 돌려주는 것뿐이라 비용이 거의 없지만, 인증 없이 열려 있는
# 엔드포인트에는 상한을 둔다는 원칙을 따른다. 가입 화면이 두 문서를 함께 부르므로
# 넉넉하게 잡는다 — 한 IP에서 분당 30번 화면을 열어도 걸리지 않는다.
@limiter.limit("60/minute")
def get_legal_document(request: Request, document: str):
    """`document`는 "terms"(이용약관) 또는 "privacy"(개인정보처리방침)."""
    if document not in DOCUMENTS:
        # 같은 디렉터리의 README.md 같은 파일이 이름만 맞히면 나가지 않도록,
        # 파일을 열어보기 전에 허용 목록으로 먼저 거른다.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="약관 문서를 찾을 수 없습니다.")

    doc = load_document(document)
    return ApiResponse[LegalDocumentResponse](
        data=LegalDocumentResponse(
            document=doc.document, title=doc.title,
            version=doc.version, content=doc.content))
