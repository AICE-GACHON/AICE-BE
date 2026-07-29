from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    .env 파일에서 값을 읽어오는 설정 클래스.
    실제 값은 .env 파일에 넣고, 이 파일에는 "어떤 값이 필요한지"만 정의합니다.
    """
    # extra="ignore": .env에는 AI 파트가 직접 읽는 키(S2_API_KEY, ANTHROPIC_API_KEY,
    # PAPER_ASSISTANT_USE_LLM)도 함께 들어 있다. 여기 선언되지 않은 키가 있다고
    # 서버 기동이 실패하면 안 되므로 무시한다.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 백엔드와 AI 파트가 공유하는 단일 DB. pgvector가 설치된 인스턴스여야 한다
    # (docker-compose.yml이 띄우는 5433 포트). 자세한 내용은 .env.example 참고.
    DATABASE_URL: str

    # 프론트엔드 개발 서버 주소들 (콤마로 구분). 배포 시 실제 프론트 도메인으로 교체.
    # 5174/5175는 vite가 5173 충돌 시 자동으로 올라가는 포트라 함께 열어둔다.
    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
    ]

    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14

    # Google 로그인용 OAuth 클라이언트 ID (Google Cloud Console에서 발급).
    # id_token 검증 시 audience로 쓰인다. 비어 있으면 POST /api/auth/google이
    # 항상 401을 반환한다 (google_oauth.verify_google_id_token).
    GOOGLE_CLIENT_ID: str = ""

    # OpenReview 계정 — 논문 코퍼스 재수집 배치(scripts/, paper_assistant/ingest/)에서만
    # 쓴다. 코퍼스는 이미 적재돼 있어 서버 운영에는 비어 있어도 된다.
    # 실제로 이 값을 읽는 쪽은 paper_assistant/config.py다.
    OPENREVIEW_USERNAME: str = ""
    OPENREVIEW_PASSWORD: str = ""

    # True면 분석 시 실제 LLM(Haiku 추출 + Sonnet 종합)을 호출하고, False면 결정론적
    # 스텁으로 돌아 비용이 0입니다. 기본은 off — 켜기 전에 팀 예산을 확인하세요.
    # paper_assistant도 같은 환경변수를 읽지만, 백엔드는 이 값을 analyze()에 명시적으로
    # 넘겨서 "이 결과가 LLM으로 만들어졌는지"(explanation_source)를 정확히 기록합니다.
    PAPER_ASSISTANT_USE_LLM: bool = False


settings = Settings()
