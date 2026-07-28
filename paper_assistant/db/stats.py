"""미리 계산해둔 코퍼스 통계 조회 (aspect base rate + venue 기준선).

둘 다 성격이 같다 — 수집이 끝난 뒤 배치가 채워 넣는 작은 테이블이고, 쿼리 시점에
매번 96만 건을 집계하지 않으려고 프로세스 안에 캐시한다. 그래서 한 모듈에 둔다.

**base rate가 왜 필요한가 (설계서 §18)**
빈도만 세면 코퍼스 base rate가 높은 aspect가 항상 1등이 된다. 코퍼스의 78.8%가
baselines 지적을 받는데 "20편 중 17편"만 보여주면 이게 이 주제 특유의 문제인지
ML 논문의 상수인지 구분할 수 없다. lift(관측률 ÷ base rate)로 재정렬해야 한다.

**venue 기준선이 왜 필요한가 (설계서 §19)**
rating 원점수는 척도가 venue마다 다르고(ICLR 2020은 1~8), 같은 6.0도 venue별로
의미가 다르다. 항상 '기준선 대비'로만 말한다. `is_coverage_biased`인 venue
(NeurIPS: 코퍼스의 95%가 accept)는 accept율·당락 경계 자체를 신뢰할 수 없다 —
OpenReview 공개 정책의 산물이지 실제 채택률이 아니다.

테이블이 비어 있으면 빈 결과를 돌려주고 파이프라인은 그 맥락 없이 동작한다
(scripts/build_base_rates.py, scripts/build_venue_stats.py로 채운다).
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


class _Cached:
    """조회 1회분을 프로세스에 캐시한다.

    **실패는 캐시하지 않는다.** 서버는 오래 떠 있는 프로세스라, DB가 잠깐
    흔들렸을 때 빈 결과를 박아두면 재시작 전까지 lift와 rating 맥락이 영영
    사라진다. 성공했을 때만 값을 남기고 실패하면 다음 호출에서 다시 시도한다.
    """

    def __init__(self, name: str, loader, hint: str):
        self._name = name
        self._loader = loader
        self._hint = hint
        self._value = None
        self._warned_empty = False

    def get(self, refresh: bool = False):
        if self._value is not None and not refresh:
            return self._value
        try:
            value = self._loader()
        except Exception as exc:               # 테이블 미생성, 일시적 DB 장애 등
            log.warning("%s 조회 실패 — 이 맥락 없이 진행합니다 (다음 호출에서 재시도): %s",
                        self._name, exc)
            # 실패를 캐시하지 않는다. 직전에 성공해 둔 값이 있으면 그걸 유지한다.
            return self._value if self._value is not None else {}
        if not value and not self._warned_empty:
            self._warned_empty = True
            log.warning("%s가 비어 있습니다. %s를 먼저 실행하세요.", self._name, self._hint)
        self._value = value
        return value

    def clear(self) -> None:
        self._value = None
        self._warned_empty = False


def _query_base_rates(sentiment: str = "weakness") -> dict[str, float]:
    with cursor() as cur:
        cur.execute(
            "SELECT aspect, base_rate FROM aspect_base_rates WHERE sentiment = %s",
            (sentiment,))
        return {row[0]: float(row[1]) for row in cur.fetchall()}


def _query_venue_stats() -> dict[str, VenueStat]:
    with cursor() as cur:
        cur.execute(
            "SELECT venue, papers, accept_rate, rating_mean, rating_sd, "
            "accept_rating_mean, reject_rating_mean, scale_max, "
            "threshold_50, is_coverage_biased FROM venue_stats")
        return {r[0]: VenueStat(*r) for r in cur.fetchall()}


_base_rates = _Cached("aspect_base_rates", _query_base_rates,
                      "scripts/build_base_rates.py")
_venue_stats = _Cached("venue_stats", _query_venue_stats,
                       "scripts/build_venue_stats.py")


def load_base_rates(refresh: bool = False) -> dict[str, float]:
    """{aspect: 코퍼스 전체 weakness 지적률(0~1)}. 조회 실패/미적재면 {}."""
    return _base_rates.get(refresh)


def load_venue_stats(refresh: bool = False) -> dict[str, VenueStat]:
    """{venue: VenueStat}. 조회 실패/미적재면 {}."""
    return _venue_stats.get(refresh)


def clear_cache() -> None:
    """테스트/배치 재실행용 — 다음 조회에서 DB를 다시 읽게 한다."""
    _base_rates.clear()
    _venue_stats.clear()


def conference_of(venue: str) -> str:
    """'ICLR 2024' -> 'ICLR'. 학회 단위 집계용."""
    return venue.split(" ")[0] if venue else venue
