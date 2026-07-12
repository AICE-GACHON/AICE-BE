from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    .env 파일에서 값을 읽어오는 설정 클래스.
    실제 값은 .env 파일에 넣고, 이 파일에는 "어떤 값이 필요한지"만 정의합니다.
    """
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    DATABASE_URL: str

    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14

    CODEF_CLIENT_ID: str = ""
    CODEF_CLIENT_SECRET: str = ""

    KAKAO_REST_API_KEY: str = ""


settings = Settings()
