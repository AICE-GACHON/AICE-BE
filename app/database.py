from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.core.config import settings

# DB 접속 엔진 생성 (실제 DB 서버와의 연결 통로)
engine = create_engine(settings.DATABASE_URL)

# DB 세션(작업 단위) 생성기
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 모든 모델(테이블 클래스)이 상속받을 기본 클래스
Base = declarative_base()


def get_db():
    """
    FastAPI 라우터에서 Depends(get_db)로 사용하는 DB 세션 제공 함수.
    요청 하나당 세션 하나를 열고, 끝나면 자동으로 닫아줍니다.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
