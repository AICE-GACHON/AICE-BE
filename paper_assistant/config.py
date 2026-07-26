import os
import re
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

OPENREVIEW_USERNAME = os.getenv("OPENREVIEW_USERNAME", "")
OPENREVIEW_PASSWORD = os.getenv("OPENREVIEW_PASSWORD", "")
S2_API_KEY = os.getenv("S2_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# docker-compose.yml 의 기본값과 일치. 포트는 로컬 Postgres와 충돌을 피해 5433.
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://paper:paper@localhost:5433/paper_assistant")

DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"


def db_url_safe() -> str:
    """로그 출력용 — 비밀번호를 가린 DB URL."""
    return re.sub(r"//([^:]+):[^@]*@", r"//\1:***@", DATABASE_URL)
