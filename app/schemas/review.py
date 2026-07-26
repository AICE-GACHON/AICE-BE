"""기존 논문이 받은 리뷰 조회 응답 스키마.

paper.py와 같은 이유로 AI 파트의 스키마를 그대로 씁니다.

통합 전과 달라진 점:
  - ReviewResponse: reviewer_label / content 대신 summary·strengths·weaknesses·questions로
    나뉩니다. OpenReview 리뷰가 원래 이 4개 섹션 구조이고, 리뷰어 식별자는 익명이라
    저장하지 않습니다. rating은 int가 아니라 float입니다 (8.5 같은 평균값이 있습니다).
    ⚠️ 2023년 이전 학회는 강/약점이 분리되지 않아 weaknesses에 리뷰 본문 전체가
    들어옵니다. 이때 is_unsplit=True이므로 프론트는 '리뷰 본문' 한 덩어리로 표시해야 합니다.
  - RevisionResponse: 여기서 빠졌습니다. "리뷰 이후 어떻게 수정됐는지"는 DB에
    저장하지 않고(papers는 최신 버전만 upsert) OpenReview API를 실시간 조회하므로,
    GET /api/papers/{paper_id}/revisions와 app/schemas/paper.py의 RevisionsResponse로
    옮겼습니다. 학회를 옮겨 다시 낸 흐름(ICLR reject → NeurIPS accept)은 그와 별개로
    분석 결과의 resubmission_flows에 있습니다.
"""
from paper_assistant.schemas import ResubmissionFlow, ReviewDetail

# 백엔드 응답 이름으로 쓰는 별칭
ReviewResponse = ReviewDetail

__all__ = ["ReviewResponse", "ReviewDetail", "ResubmissionFlow"]
