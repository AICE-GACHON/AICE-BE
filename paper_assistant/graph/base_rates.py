"""코퍼스 전체 aspect base rate 조회.

쿼리 시점 리뷰 패턴은 base rate 없이는 해석이 불가능하다 — 코퍼스의 78.8%가
baselines 지적을 받는데 "20편 중 17편"만 보여주면 사용자는 이게 이 주제 특유의
문제인지 ML 논문의 상수인지 구분할 수 없다 (설계서 §18).

값은 `aspect_base_rates` 테이블에 미리 계산해둔다 (scripts/build_base_rates.py).
매 쿼리마다 96만 건을 집계할 이유가 없고, 9행짜리라 프로세스 캐시로 충분하다.
테이블이 없으면 빈 dict를 반환해 lift 없이 빈도순으로 폴백한다.
"""
import logging

from paper_assistant.db.connection import cursor

log = logging.getLogger(__name__)

_cache: dict[str, float] | None = None


def load_base_rates(sentiment: str = "weakness",
                    refresh: bool = False) -> dict[str, float]:
    """{aspect: 코퍼스 전체 지적률(0~1)}. 테이블이 없으면 {}."""
    global _cache
    if _cache is not None and not refresh:
        return _cache

    try:
        with cursor() as cur:
            cur.execute(
                "SELECT aspect, base_rate FROM aspect_base_rates "
                "WHERE sentiment = %s",
                (sentiment,))
            _cache = {row[0]: float(row[1]) for row in cur.fetchall()}
    except Exception as exc:                      # 테이블 미생성 등
        log.warning("base rate 조회 실패 — lift 없이 빈도순으로 폴백합니다: %s", exc)
        _cache = {}

    if not _cache:
        log.warning("aspect_base_rates가 비어 있습니다. "
                    "scripts/build_base_rates.py를 먼저 실행하세요.")
    return _cache
