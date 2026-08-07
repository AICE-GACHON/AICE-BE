import uuid

from pydantic import Field

from app.schemas.common import ORMBase, TimestampMixin


class SubmissionResponse(ORMBase, TimestampMixin):
    """업로드한 내 논문 1편.

    ⚠️ **pdf_bytes를 절대 넣지 마세요.** 최대 20MB짜리 블롭이라 응답에 실리면
    base64로 부풀어 매 조회가 수십 MB가 됩니다. PDF는 서버가 분석할 때만 읽습니다.
    """
    submission_id: uuid.UUID
    user_id: uuid.UUID
    title: str
    abstract: str
    content: str | None
    field: str | None
    page_count: int | None = Field(
        default=None,
        description="PDF 페이지 수. 프론트는 이 값이 15를 넘으면 '논문 PDF가 맞는지' "
                    "확인 창을 띄우고, 사용자가 확인해야 분석을 시작할 것. 60 초과는 "
                    "서버가 업로드 단계에서 이미 거부했으므로 여기 오지 않는다.")


class SubmissionSummary(ORMBase, TimestampMixin):
    """목록 응답 — 본문(content)과 초록을 싣지 않아 가볍습니다."""
    submission_id: uuid.UUID
    title: str
    field: str | None


class SimilarPaperMatchResponse(ORMBase, TimestampMixin):
    """검색 후보 1편, 그리고 그것이 최종 선정됐는지.

    **후보 50편이 전부 내려옵니다.** 사용자에게 보여줄 것은 `selected=true`인 5편뿐이고,
    나머지는 근거 추적·품질 측정용입니다 — 프론트는 반드시 selected로 걸러 쓰세요.

    similarity_score가 없는 것은 의도한 설계입니다 (app/models/analysis.py 주석 참고).
    상세 정보는 paper_id로 GET /api/papers/{paper_id}를 호출해 가져옵니다.
    """
    match_id: uuid.UUID
    prediction_id: uuid.UUID
    paper_id: int          # 코퍼스 papers.id (BIGINT)
    rank: int              # **검색 순위**(1~50). 화면 순서가 아닙니다
    match_type: str        # both | semantic | lexical

    selected: bool = Field(
        default=False, description="LLM이 최종 선정했는가. 화면에는 이것이 true인 것만")
    llm_rank: int | None = Field(
        default=None, description="선정된 논문의 표시 순서(1~5). 미선정이면 None")
    selection_reason: str | None = Field(
        default=None, description="LLM이 이 논문을 고른 이유. 미선정이면 None")
