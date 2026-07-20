# 이 파일에서 모든 모델을 한 번씩 import 해줘야
# Alembic이 "테이블이 뭐가 있는지" 인식할 수 있습니다.
from app.models.user import User
from app.models.paper import Paper
from app.models.review import Review, Revision
from app.models.submission import Submission, SimilarPaperMatch
from app.models.feedback import ReviewPrediction

__all__ = [
    "User",
    "Paper",
    "Review",
    "Revision",
    "Submission",
    "SimilarPaperMatch",
    "ReviewPrediction",
]
