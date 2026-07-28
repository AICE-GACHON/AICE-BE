"""수집 클라이언트 3종(OpenReview / Semantic Scholar / arXiv)이 공유하는 HTTP 뼈대.

세 API의 재시도 조건은 서로 다르다 — arXiv는 503 + Retry-After, S2는 429와 5xx,
OpenReview는 429와 401(토큰 만료 후 재인증)이다. 그래서 "무엇을 재시도할지"는
호출자가 `decide`로 정하고, 여기서는 세 곳에서 똑같이 반복되던 부분만 맡는다:
요청 → 대기 → 재시도 → 다 소진하면 같은 모양의 예외.
"""
import logging
import time
from collections.abc import Callable

import requests

USER_AGENT = "paper-assistant/0.1 (academic project)"

log = logging.getLogger(__name__)

# decide(response, attempt) -> 대기 초. None을 주면 "이 응답을 그대로 쓴다"는 뜻.
Decide = Callable[[requests.Response, int], float | None]


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def request_with_retry(session: requests.Session, method: str, url: str, *,
                       decide: Decide, retries: int, timeout: float,
                       label: str = "", before: Callable[[], None] | None = None,
                       **kwargs) -> requests.Response:
    """재시도 정책을 입혀 요청을 보낸다.

    decide가 None을 돌려주면 그 응답을 raise_for_status 후 반환한다. 숫자를
    돌려주면 그만큼 자고 다시 보낸다. `before`는 매 시도 직전 훅이다 (레이트리밋
    간격 유지 등).
    """
    response = None
    for attempt in range(retries):
        if before is not None:
            before()
        response = session.request(method, url, timeout=timeout, **kwargs)
        wait = decide(response, attempt)
        if wait is None:
            response.raise_for_status()
            return response
        if wait > 0:
            time.sleep(wait)
    status = response.status_code if response is not None else "?"
    raise RuntimeError(
        f"{label or url}: {retries}회 재시도 후에도 실패 (마지막 {status})")
