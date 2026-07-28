"""백엔드 설정.

여기에는 **백엔드 전용 값만** 선언한다. DB 주소와 LLM 토글처럼 AI 파트와 공유하는
값은 `paper_assistant.config`가 단일 소스이고, 아래에서 그대로 읽어 쓴다 —
같은 키를 두 곳에 선언하면 한쪽만 고쳤을 때 조용히 다른 DB를 보게 된다.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict

from paper_assistant import config as shared


class Settings(BaseSettings):
    """.env에서 값을 읽어오는 설정 클래스.

    extra="ignore": .env에는 AI 파트가 읽는 키(S2_API_KEY, OPENREVIEW_* 등)도 함께
    들어 있다. 여기 선언되지 않은 키가 있다고 서버 기동이 실패하면 안 된다.
    """
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 프론트엔드 개발 서버 주소들. 배포 시 실제 프론트 도메인으로 교체.
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # 기동 시 SPECTER2를 미리 로드할지. 켜면 첫 분석이 빨라지지만 매 기동이
    # 수십 초 느려진다 — 로컬 개발/테스트에서는 꺼두는 편이 낫다.
    WARMUP_ON_STARTUP: bool = False

    # --- AI 파트와 공유하는 값 (선언은 paper_assistant/config.py 한 곳뿐) ---

    @property
    def DATABASE_URL(self) -> str:
        """백엔드와 AI 파트가 함께 쓰는 단일 DB. pgvector가 설치된 인스턴스여야 한다
        (docker-compose.yml이 띄우는 5433 포트). 자세한 내용은 .env.example 참고."""
        return shared.DATABASE_URL

    @property
    def USE_LLM(self) -> bool:
        """분석 시 실제 LLM을 호출할지. 기본 off($0)."""
        return shared.USE_LLM


settings = Settings()
