from pydantic import BaseModel, Field


class LegalDocumentResponse(BaseModel):
    """약관/개인정보처리방침 원문 (GET /api/legal/{document})."""

    document: str = Field(description='"terms" 또는 "privacy"')
    title: str = Field(description="문서 첫 줄의 제목. 화면 상단에 그대로 쓰면 된다")
    version: str = Field(
        description="게시 중인 버전. 회원가입 시 이 버전이 동의 이력으로 저장된다")
    # 마크다운 그대로 내보낸다. 서버에서 HTML로 렌더하지 않는 이유는 두 가지다 —
    # (1) 화면마다 필요한 스타일이 다르고, (2) 서버가 HTML을 만들면 그 HTML을
    # 어디에 어떻게 넣을지가 프론트의 XSS 판단거리가 된다. 마크다운은 프론트가
    # 이미 쓰는 렌더러로 안전하게 처리하면 된다.
    format: str = Field(default="markdown", description="content의 형식. 항상 markdown")
    content: str = Field(description="문서 원문 (마크다운)")
