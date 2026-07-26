"""Postgres 연결 관리.

백엔드 팀이 이 패키지를 import할 때 자체 커넥션 풀을 쓸 수도 있으므로,
연결 생성은 한 곳(get_pool)에만 두고 나머지 코드는 커서만 받아 쓴다.
"""
import atexit
import logging
from contextlib import contextmanager

from psycopg_pool import ConnectionPool
from pgvector.psycopg import register_vector

from paper_assistant import config

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def _configure(conn):
    """새 커넥션마다 vector 타입 어댑터를 등록한다."""
    register_vector(conn)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        log.info("DB 커넥션 풀 생성: %s", config.db_url_safe())
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=8,
                               configure=_configure, open=True)
        # Python 3.14는 인터프리터 종료 시점의 스레드 join을 막는다.
        # 풀을 명시적으로 닫지 않으면 PythonFinalizationError가 발생한다.
        atexit.register(close_pool)
    return _pool


@contextmanager
def cursor(commit: bool = False):
    """커서 컨텍스트 매니저.

        with cursor(commit=True) as cur:
            cur.execute("INSERT ...")
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            yield cur
        if commit:
            conn.commit()


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
