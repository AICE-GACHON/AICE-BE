# 이 파일에서 모든 모델을 한 번씩 import 해줘야
# Alembic이 "테이블이 뭐가 있는지" 인식할 수 있습니다.
from app.models.user import User, AuthCredential, UserConsent
from app.models.card import Card, UserCard
from app.models.performance import CodefConnection, CardPerformance
from app.models.merchant import Merchant, CategoryMapping
from app.models.recommendation import BenefitClause, Recommendation

__all__ = [
    "User",
    "AuthCredential",
    "UserConsent",
    "Card",
    "UserCard",
    "CodefConnection",
    "CardPerformance",
    "Merchant",
    "CategoryMapping",
    "BenefitClause",
    "Recommendation",
]
