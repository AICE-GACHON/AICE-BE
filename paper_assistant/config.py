"""환경설정 — 저장소 전체의 **단일 소스**.

백엔드(app/)와 AI 파트(paper_assistant/)가 같은 `.env`를 쓰므로, 공유 키의 이름과
기본값은 여기에만 둔다. `app/core/config.py`의 Settings는 JWT·CORS 같은 백엔드
전용 값만 선언하고 아래 값들은 이 모듈을 그대로 읽는다 — 같은 키를 두 곳에
선언하면 한쪽만 고쳤을 때 조용히 다른 DB를 보게 된다.

의존 방향은 app → paper_assistant 한 방향이다 (그 반대는 없다).
"""
import os
import re
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- DB
# docker-compose.yml 의 기본값과 일치. 포트는 로컬 Postgres와 충돌을 피해 5433.
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://paper:paper@localhost:5433/paper_assistant")

# ------------------------------------------------------------ 분석(LLM)
# True면 분석 시 실제 LLM(Haiku 태깅 + Sonnet 종합)을 호출하고, False면 결정론적
# 스텁으로 돌아 비용이 0이다. 기본 off — 켜기 전에 팀 예산을 확인할 것.
# ANTHROPIC_API_KEY는 anthropic SDK가 환경변수에서 직접 읽으므로 여기서 다루지 않는다.
USE_LLM = _flag("PAPER_ASSISTANT_USE_LLM", False)

# ------------------------------------------------------------ 수집 배치
# 아래 셋은 논문 코퍼스 재수집(scripts/, paper_assistant/ingest/)에만 필요하다.
# 코퍼스는 이미 적재돼 있어 서버 운영에는 비어 있어도 된다.
OPENREVIEW_USERNAME = os.getenv("OPENREVIEW_USERNAME", "")
OPENREVIEW_PASSWORD = os.getenv("OPENREVIEW_PASSWORD", "")
S2_API_KEY = os.getenv("S2_API_KEY", "")

DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"


def db_url_safe() -> str:
    """로그 출력용 — 비밀번호를 가린 DB URL."""
    return re.sub(r"//([^:]+):[^@]*@", r"//\1:***@", DATABASE_URL)
