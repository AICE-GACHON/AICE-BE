from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.core.config import settings


def sqlalchemy_url(url: str) -> str:
    """SQLAlchemy가 psycopg3을 쓰도록 URL 스킴을 바꾼다.

    .env의 DATABASE_URL은 백엔드와 AI 파트가 함께 쓰는 값이라 순수 libpq 형식
    (postgresql://...)이어야 한다 — AI 파트의 psycopg3 커넥션 풀은 SQLAlchemy 방언
    접미사(+psycopg)를 이해하지 못한다. 그래서 .env에는 접미사 없이 적어두고,
    SQLAlchemy로 넘길 때만 여기서 붙인다.

    드라이버를 psycopg3 하나로 통일한 이유: AI 파트가 pgvector 어댑터 등록에
    psycopg3을 쓰는데 psycopg2까지 같이 깔면 커넥션 풀과 드라이버가 두 벌이 된다.
    """
    if url.startswith("postgresql+"):      # 이미 방언이 지정된 경우 그대로 둔다
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):      # 일부 호스팅이 쓰는 축약형
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url


# DB 접속 엔진 생성 (실제 DB 서버와의 연결 통로)
engine = create_engine(sqlalchemy_url(settings.DATABASE_URL))

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
