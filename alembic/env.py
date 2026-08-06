import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# 프로젝트 루트를 import 경로에 추가 (app 패키지를 찾을 수 있도록)
sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.database import Base, sqlalchemy_url  # noqa: E402
import app.models  # noqa: E402,F401  # 모든 모델을 등록하기 위해 import만 해줌

# AI 파트가 scripts/init_db.sql로 관리하는 논문 코퍼스 테이블.
# 같은 DB에 있지만 app/models에는 없으므로, 이 목록을 걸러주지 않으면
# autogenerate가 "모델에 없는 테이블"로 보고 전부 DROP하는 마이그레이션을 만든다.
#
# ⚠️ **코퍼스 테이블을 새로 만들면 여기에도 반드시 추가할 것.** 빠뜨려도 아무 일이
# 일어나지 않다가, 다음에 누군가 autogenerate를 돌리는 순간 그 테이블을 DROP하는
# 마이그레이션이 조용히 만들어진다. `alembic check`가 이 상태를 잡아준다.
CORPUS_TABLES = {
    "papers",
    "authors",
    "paper_authors",
    "reviews",
    "review_points",
    "aspect_base_rates",
    "venue_stats",
    "citations",
    "submission_links",
    "ingest_status",
    # 심사 서사 캐시. alembic 0009가 만들지만 소유자는 AI 파트이고 SQLAlchemy 모델이
    # 없다 — 백엔드는 이 테이블을 직접 읽지 않고 paper_assistant를 거친다.
    "paper_stories",
}


def include_object(obj, name, type_, reflected, compare_to):
    """코퍼스 테이블(과 그 인덱스)은 alembic의 관리 대상에서 제외한다."""
    if type_ == "table":
        return name not in CORPUS_TABLES
    if type_ == "index" and obj.table is not None:
        return obj.table.name not in CORPUS_TABLES
    return True

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# .env에서 읽어온 DB 주소를 alembic 설정에 주입.
# app/database.py와 같은 함수를 써서 psycopg3 방언(postgresql+psycopg)으로 맞춘다.
config.set_main_option("sqlalchemy.url", sqlalchemy_url(settings.DATABASE_URL))

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 'autogenerate'가 우리 모델(app/models)을 인식하도록 연결
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
