"""서비스 테이블 모델.

여기서 모든 모델을 한 번씩 import 해줘야 Alembic이 테이블을 인식합니다.

논문 코퍼스 테이블(papers, reviews, review_points, venue_stats …)의 모델은
여기 없습니다. AI 파트가 scripts/init_db.sql로 관리하고 psycopg3으로 직접 읽습니다.
코퍼스 조회가 필요하면 paper_assistant의 공개 함수를 쓰세요.
(alembic/env.py의 CORPUS_TABLES가 autogenerate 대상에서도 제외합니다.)
"""
from app.models.analysis import IN_PROGRESS, ReviewPrediction, SimilarPaperMatch
from app.models.onboarding import OnboardingProfile
from app.models.share import SubmissionShare
from app.models.submission import Submission
from app.models.user import User

__all__ = [
    "User",
    "Submission",
    "SubmissionShare",
    "ReviewPrediction",
    "SimilarPaperMatch",
    "IN_PROGRESS",
    "OnboardingProfile",
]
