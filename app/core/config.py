"""백엔드 설정.

여기에는 **백엔드 전용 값만** 선언한다. DB 주소와 LLM 토글처럼 AI 파트와 공유하는
값은 `paper_assistant.config`가 단일 소스이고, 아래에서 그대로 읽어 쓴다 —
같은 키를 두 곳에 선언하면 한쪽만 고쳤을 때 조용히 다른 DB를 보게 된다.
"""
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from paper_assistant import config as shared

# .env.example이 그대로 배포되는 사고를 막기 위한 목록. 이 값들은 공개 저장소에
# 적혀 있어서, 프로덕션에 남아 있으면 누구나 모든 계정의 토큰을 위조할 수 있다.
_PLACEHOLDER_SECRETS = {
    "change-this-to-a-random-secret-key",
    "secret",
    "changeme",
    "your-secret-key",
}
# HS256 서명 키의 실질 하한. 128비트 미만은 오프라인 무차별 대입의 사정권이다.
_MIN_SECRET_LEN = 32


class Settings(BaseSettings):
    """.env에서 값을 읽어오는 설정 클래스.

    extra="ignore": .env에는 AI 파트가 읽는 키(S2_API_KEY, OPENREVIEW_* 등)도 함께
    들어 있다. 여기 선언되지 않은 키가 있다고 서버 기동이 실패하면 안 된다.
    """
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 실행 환경. production이면 아래 model_validator가 개발용 기본값(placeholder
    # JWT 키, localhost CORS, 열린 /docs)을 **기동 실패로** 걸러낸다 — 이런 것들은
    # 조용히 넘어가면 배포 후에야 드러나고, 그때는 이미 사고다.
    ENVIRONMENT: Literal["development", "production"] = "development"

    # 프론트엔드 개발 서버 주소들. 배포 시 실제 프론트 도메인으로 교체.
    # 5174/5175는 vite가 5173 충돌 시 자동으로 올라가는 포트라 함께 열어둔다.
    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
    ]

    # Host 헤더 허용 목록 (TrustedHostMiddleware). Host 헤더를 그대로 믿으면
    # 캐시 오염과 비밀번호 재설정 링크 위조의 발판이 된다. production에서는
    # 반드시 실제 도메인으로 채워야 한다 — 비어 있으면 기동을 거부한다.
    ALLOWED_HOSTS: list[str] = ["*"]

    # Swagger(/docs)·ReDoc·OpenAPI 스키마를 열어둘지. 스키마는 전체 엔드포인트와
    # 요청 형식을 그대로 알려주는 공격 지도라, production에서는 **명시하지 않으면
    # 자동으로 닫힌다** (아래 _close_docs_in_production). 열려면 ENABLE_DOCS=1을
    # .env에 직접 적어야 한다 — 그 선택이 기록으로 남게.
    ENABLE_DOCS: bool = True

    # rate limit 저장소. 비우면 프로세스 메모리라 **워커마다 따로 센다** —
    # uvicorn --workers 4면 실제 상한이 4배가 된다. 워커를 늘릴 거면
    # redis://... 를 넣어야 상한이 실제 상한이 된다 (app/core/rate_limit.py).
    RATE_LIMIT_STORAGE_URI: str = ""

    # 요청 본문 상한 (bytes). PDF 업로드(20MB)가 가장 큰 정상 요청이라 여유를
    # 조금 얹었다. 이 상한이 없으면 어떤 엔드포인트든 클라이언트가 보내는 만큼
    # 메모리에 쌓인다 (app/core/middleware.py).
    MAX_REQUEST_BYTES: int = 24 * 1024 * 1024

    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14

    # 기동 시 SPECTER2를 미리 로드할지. 켜면 첫 분석이 빨라지지만 매 기동이
    # 수십 초 느려진다 — 로컬 개발/테스트에서는 꺼두는 편이 낫다.
    WARMUP_ON_STARTUP: bool = False

    # Google 로그인용 OAuth 클라이언트 ID (Google Cloud Console에서 발급).
    # id_token 검증 시 audience로 쓰인다. 비어 있으면 POST /api/auth/google이
    # 항상 401을 반환한다 (google_oauth.verify_google_id_token).
    GOOGLE_CLIENT_ID: str = ""

    # OpenReview 계정 — 논문 코퍼스 재수집 배치(scripts/, paper_assistant/ingest/)에서만
    # 쓴다. 코퍼스는 이미 적재돼 있어 서버 운영에는 비어 있어도 된다.
    # 실제로 이 값을 읽는 쪽은 paper_assistant/config.py다.
    OPENREVIEW_USERNAME: str = ""
    OPENREVIEW_PASSWORD: str = ""

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

    # ------------------------------------------------------------ 배포 안전장치

    @model_validator(mode="after")
    def _reject_insecure_secret(self) -> "Settings":
        """개발용 placeholder 서명 키는 **어느 환경에서도** 거부한다.

        JWT_SECRET_KEY는 모든 계정의 신원 그 자체다. 이 값이 새면 공격자가 임의
        user_id로 access_token을 만들어 아무 계정으로나 로그인할 수 있고, 서버는
        그것을 정상 로그인과 구분할 방법이 없다. .env.example의 문자열은 공개
        저장소에 적혀 있으므로 '아직 안 바꿨다'와 '유출됐다'가 같은 뜻이다.

        development에서도 막는 이유: 개발 DB로 시작한 인스턴스가 그대로 배포되는
        경로가 실제로 흔하다. 여기서 한 번 막으면 그 경로가 사라진다.
        """
        if self.JWT_SECRET_KEY.strip().lower() in _PLACEHOLDER_SECRETS:
            raise ValueError(
                "JWT_SECRET_KEY가 .env.example의 예시 값 그대로입니다. "
                "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"` 로 "
                "새 값을 만들어 .env에 넣으세요."
            )
        if len(self.JWT_SECRET_KEY) < _MIN_SECRET_LEN:
            raise ValueError(
                f"JWT_SECRET_KEY가 너무 짧습니다 ({len(self.JWT_SECRET_KEY)}자). "
                f"{_MIN_SECRET_LEN}자 이상을 쓰세요."
            )
        return self

    @model_validator(mode="after")
    def _close_docs_in_production(self) -> "Settings":
        """production에서 ENABLE_DOCS를 명시하지 않았으면 문서를 닫는다.

        기본값을 False로 두지 않는 이유: 로컬 개발에서 /docs는 사실상 필수라
        전원이 매번 켜야 하고, 그러면 "켜는 것"이 습관이 되어 배포에도 딸려간다.
        환경에 따라 기본값이 다른 편이 양쪽 모두에서 옳다.
        """
        if self.ENVIRONMENT == "production" and "ENABLE_DOCS" not in self.model_fields_set:
            self.ENABLE_DOCS = False
        return self

    @model_validator(mode="after")
    def _reject_dev_defaults_in_production(self) -> "Settings":
        """production에서 개발용 기본값이 남아 있으면 기동을 거부한다.

        경고 로그로 두지 않는 이유: 기동 로그는 아무도 읽지 않는다. 이 값들은
        전부 '틀리면 공개되는' 종류라, 조용히 뜨는 것보다 안 뜨는 편이 낫다.
        """
        if self.ENVIRONMENT != "production":
            return self

        problems: list[str] = []

        if "*" in self.CORS_ORIGINS:
            # allow_credentials=True와 함께라면 브라우저가 거부하지만, 설정이
            # 살아 있는 한 자격증명 없는 요청에는 그대로 열린다.
            problems.append("CORS_ORIGINS에 와일드카드('*')를 쓸 수 없습니다.")
        insecure = [o for o in self.CORS_ORIGINS
                    if "localhost" in o or "127.0.0.1" in o or o.startswith("http://")]
        if insecure:
            problems.append(
                f"CORS_ORIGINS에 개발용/평문 origin이 남아 있습니다: {insecure}. "
                "실제 프론트 도메인(https://)만 남기세요.")

        if "*" in self.ALLOWED_HOSTS:
            problems.append(
                "ALLOWED_HOSTS가 '*'입니다. 서비스할 도메인을 명시하세요 "
                '(예: ALLOWED_HOSTS=["api.example.com"]).')

        if problems:
            raise ValueError(
                "ENVIRONMENT=production 설정이 안전하지 않습니다:\n  - "
                + "\n  - ".join(problems))
        return self


settings = Settings()
