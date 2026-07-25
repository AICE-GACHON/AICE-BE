from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    .env 파일에서 값을 읽어오는 설정 클래스.
    실제 값은 .env 파일에 넣고, 이 파일에는 "어떤 값이 필요한지"만 정의합니다.
    """
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    DATABASE_URL: str

    # 프론트엔드 개발 서버 주소들 (콤마로 구분). 배포 시 실제 프론트 도메인으로 교체.
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14

    # OpenReview API (papers/reviews/revisions 수집용, openreview-py 사용 예정)
    OPENREVIEW_USERNAME: str = ""
    OPENREVIEW_PASSWORD: str = ""


settings = Settings()
