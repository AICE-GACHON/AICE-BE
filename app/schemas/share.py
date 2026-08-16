from pydantic import BaseModel, Field

from app.schemas.common import ORMBase
from paper_assistant.schemas import Report


class ShareLinkResponse(BaseModel):
    """공유 링크 발급/조회 응답 (소유자만 본다)."""
    token: str = Field(description="공개 조회 토큰. 이 값을 아는 사람은 로그인 없이 "
                                   "결과를 볼 수 있으므로 링크 외의 경로로 노출하지 말 것")
    url: str = Field(description="프론트 공개 뷰 주소. 서버가 FRONTEND_BASE_URL로 "
                                 "조립해 주므로 프론트가 다시 만들 필요가 없다")


class SharedAnalysisResponse(ORMBase):
    """공개 조회 응답 — **비로그인 사용자가 보는 유일한 형태다.**

    ⚠️ **필드를 늘릴 때는 그것이 공개되어도 되는지 먼저 따지세요.** 이 스키마는
    편의가 아니라 경계입니다. AnalysisResponse를 그대로 재사용하지 않고 따로 만든
    이유가 그것이고, 여기에 필드를 하나 추가하는 것은 인터넷 전체에 공개하는 것과
    같습니다. 특히 아래 것들은 의도적으로 빠져 있습니다:

      - user_id·이메일·닉네임 등 소유자 정보 — 누가 올렸는지는 공유 대상이 아니다
      - submission_id·prediction_id — 로그인 경로의 자원을 가리키는 내부 식별자다.
        열리지는 않지만(그쪽은 소유자 검증이 있다) 굳이 알려줄 이유가 없다
      - pdf_bytes — 미출간 원고 원본이다. 링크를 공유한 것이지 원고를 준 것이 아니다
      - matches(후보 50편) — report.selected_papers가 화면의 주인공이고, 후보 풀은
        내부 품질 지표다

    반대로 report는 통째로 싣는다. 그 안에 있는 query_title·query_abstract는 아래
    title·abstract와 같은 값이고, 나머지는 전부 공개 논문(코퍼스) 정보다.
    """
    title: str
    abstract: str
    field: str | None = None
    report: Report | None = Field(
        default=None,
        description="분석 결과 전체. 화면에 옮길 때 주의점은 docs/DEVELOPMENT.md §6과 "
                    "같다 — confidence.level이 weak이면 공개 뷰에서도 경고를 띄워야 "
                    "하고, 유사 논문에 유사도 %를 표시하면 안 된다.")
