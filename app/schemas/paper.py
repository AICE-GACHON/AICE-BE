"""논문 코퍼스 조회 응답 스키마.

여기서는 스키마를 새로 정의하지 않고 AI 파트의 것을 그대로 씁니다. 코퍼스 테이블은
AI 파트가 소유하고(scripts/init_db.sql), 조회도 paper_assistant.get_paper_detail()로
하기 때문에, 백엔드가 같은 모양의 Pydantic 모델을 한 벌 더 만들면 필드가 어긋날 때
아무도 모르게 깨집니다.

⚠️ paper_id는 UUID가 아니라 **BIGINT**입니다. 코퍼스는 43,515편이 BIGSERIAL로 적재돼
있고, 분석 결과(Report.similar_papers[].paper_id)도 이 id를 그대로 내려줍니다.
"""
from paper_assistant.schemas import PaperDetail, ReviewDetail, ReviewPointDetail

# 백엔드 응답 이름으로 쓰는 별칭 (기존 PaperResponse 자리를 대체)
PaperResponse = PaperDetail

__all__ = ["PaperResponse", "PaperDetail", "ReviewDetail", "ReviewPointDetail"]
