"""논문 코퍼스 조회 응답 스키마 (기존 논문·리뷰·수정 이력).

여기서는 스키마를 새로 정의하지 않고 **AI 파트의 것을 그대로 재수출**합니다.
코퍼스 테이블은 AI 파트가 소유하고(scripts/init_db.sql) 조회도 paper_assistant의
공개 함수로 하기 때문에, 백엔드가 같은 모양의 Pydantic 모델을 한 벌 더 만들면
필드가 어긋날 때 아무도 모르게 깨집니다.

주의사항 두 가지:

⚠️ paper_id는 UUID가 아니라 **BIGINT**입니다. 코퍼스 43,515편이 BIGSERIAL로 적재돼
있고, 분석 결과(Report.similar_papers[].paper_id)도 이 id를 그대로 내려줍니다.

⚠️ 2023년 이전 학회 리뷰는 강점/약점이 분리되지 않은 형식이라 weaknesses에 리뷰
본문 전체가 들어옵니다. 이때 `is_unsplit=True`이므로 프론트는 '약점'이라고 라벨을
붙이지 말고 '리뷰 본문' 한 덩어리로 표시해야 합니다.
"""
from paper_assistant.schemas import (
    DiffSegment, FieldChange, PaperDetail, PaperRevisions, ReviewDetail,
    ReviewPointDetail, RevisionEntry)

# 백엔드 응답 이름으로 쓰는 별칭
PaperResponse = PaperDetail
ReviewResponse = ReviewDetail
# 수정 이력은 DB에 저장하지 않고 OpenReview API를 실시간 조회합니다.
RevisionsResponse = PaperRevisions

__all__ = [
    "PaperResponse",
    "ReviewResponse",
    "RevisionsResponse",
    "PaperDetail",
    "ReviewDetail",
    "ReviewPointDetail",
    "PaperRevisions",
    "RevisionEntry",
    "FieldChange",
    "DiffSegment",
]
