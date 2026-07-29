# 이 파일에서 모든 모델을 한 번씩 import 해줘야
# Alembic이 "테이블이 뭐가 있는지" 인식할 수 있습니다.
#
# 논문 코퍼스 테이블(papers, reviews, review_points, venue_stats …)의 모델은
# 여기 없습니다. AI 파트가 scripts/init_db.sql로 관리하고 psycopg3으로 직접 읽습니다.
# 코퍼스 조회가 필요하면 paper_assistant.get_paper_detail()을 쓰세요.
# (alembic/env.py의 CORPUS_TABLES가 autogenerate 대상에서도 제외합니다.)
from app.models.user import User
from app.models.submission import Submission, SimilarPaperMatch
from app.models.feedback import ReviewPrediction
from app.models.onboarding import OnboardingProfile

__all__ = [
    "User",
    "Submission",
    "SimilarPaperMatch",
    "ReviewPrediction",
    "OnboardingProfile",
]
