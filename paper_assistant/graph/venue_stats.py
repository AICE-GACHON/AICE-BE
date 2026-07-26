"""venue별 rating 기준선·당락 경계·표본 편향 조회.

rating은 원점수를 그대로 보여주면 안 된다 (설계서 §19). 척도가 venue마다 다르고
(ICLR 2020은 1~8), 같은 6.0도 venue별로 의미가 다르다. 여기서 읽어온 기준선으로
'평균 대비 +0.8' / '당락 경계 6.0에 못 미침' 처럼 상대적으로만 말한다.

`is_coverage_biased`가 참인 venue(NeurIPS: 코퍼스의 95%가 accept)는 accept율과
당락 경계를 신뢰할 수 없다 — OpenReview 공개 정책의 산물이지 실제 채택률이 아니다.

값은 `venue_stats` 테이블에 미리 계산해둔다 (scripts/build_venue_stats.py).
테이블이 없으면 빈 dict를 반환해 rating 맥락 없이 동작한다.
"""
import logging
from dataclasses import dataclass

from paper_assistant.db.connection import cursor

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VenueStat:
    venue: str
    papers: int
    accept_rate: float
    rating_mean: float | None
    rating_sd: float | None
    accept_rating_mean: float | None
    reject_rating_mean: float | None
    scale_max: float | None
    threshold_50: float | None
    """통과율 50%를 넘기는 최저 평균 rating. 편향 venue는 None."""
    is_coverage_biased: bool


_cache: dict[str, VenueStat] | None = None


def load_venue_stats(refresh: bool = False) -> dict[str, VenueStat]:
    """{venue: VenueStat}. 테이블이 없으면 {}."""
    global _cache
    if _cache is not None and not refresh:
        return _cache

    try:
        with cursor() as cur:
            cur.execute(
                "SELECT venue, papers, accept_rate, rating_mean, rating_sd, "
                "accept_rating_mean, reject_rating_mean, scale_max, "
                "threshold_50, is_coverage_biased FROM venue_stats")
            _cache = {r[0]: VenueStat(*r) for r in cur.fetchall()}
    except Exception as exc:                      # 테이블 미생성 등
        log.warning("venue_stats 조회 실패 — rating 맥락 없이 진행합니다: %s", exc)
        _cache = {}

    if not _cache:
        log.warning("venue_stats가 비어 있습니다. "
                    "scripts/build_venue_stats.py를 먼저 실행하세요.")
    return _cache


def conference_of(venue: str) -> str:
    """'ICLR 2024' -> 'ICLR'. 학회 단위 집계용."""
    return venue.split(" ")[0] if venue else venue
