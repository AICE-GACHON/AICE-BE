"""OpenReview API 클라이언트 (v1/v2 분기 + 토큰 캐시 + 페이지네이션 + 백오프).

주의사항 (실측으로 확인된 API 특성):
- 익명 /notes 요청은 ChallengeRequiredError(봇 검증) 403 → 로그인 필수.
- /login 자체에 rate limit이 있음 → 토큰을 디스크에 캐시해 재사용한다.
- v2는 limit=1일 때 캐시 응답을 반환하며 count 필드를 생략한다 → limit>=3 + offset 명시.
- 2023년 이전 venue는 v1(api.openreview.net)에만 존재하고 invitation 이름도
  Blind_Submission으로 다르다. VENUE_REGISTRY 참고.
"""
import json
import logging
import time

import jwt
import requests

from paper_assistant import config

V1 = "https://api.openreview.net"
V2 = "https://api2.openreview.net"
PAGE_SIZE = 1000
MAX_RETRIES = 5

log = logging.getLogger(__name__)


class OpenReviewClient:
    """단일 API 버전(v1 또는 v2)에 대한 인증된 클라이언트."""

    def __init__(self, base: str = V2, username: str | None = None,
                 password: str | None = None):
        self.base = base
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "paper-assistant/0.1 (academic project)"
        self.username = username or config.OPENREVIEW_USERNAME
        self.password = password or config.OPENREVIEW_PASSWORD
        if not self.username or not self.password:
            raise RuntimeError(
                "OpenReview 자격 증명이 없습니다. .env에 OPENREVIEW_USERNAME/"
                "OPENREVIEW_PASSWORD를 설정하세요 (.env.example 참고).")
        self._authenticate()

    # --- 인증 (토큰 디스크 캐시) ---

    @property
    def _token_path(self):
        tag = "v1" if self.base == V1 else "v2"
        return config.DATA_DIR / f".token_{tag}.json"

    def _cached_token(self) -> str | None:
        path = self._token_path
        if not path.exists():
            return None
        try:
            token = json.loads(path.read_text())["token"]
            claims = jwt.decode(token, options={"verify_signature": False})
        except Exception:
            return None
        # 만료 10분 전이면 폐기
        if claims.get("exp", 0) - time.time() < 600:
            return None
        return token

    def _authenticate(self) -> None:
        token = self._cached_token()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
            log.debug("캐시된 토큰 사용 (%s)", self.base)
            return

        for attempt in range(MAX_RETRIES):
            r = self.session.post(f"{self.base}/login",
                                  json={"id": self.username, "password": self.password},
                                  timeout=30)
            if r.status_code == 429:
                wait = 2 ** attempt * 10
                log.warning("로그인 rate limit, %ds 대기", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            token = r.json()["token"]
            self.session.headers["Authorization"] = f"Bearer {token}"
            self._token_path.parent.mkdir(parents=True, exist_ok=True)
            self._token_path.write_text(json.dumps({"token": token}))
            log.info("OpenReview 로그인 성공: %s (%s)", self.username, self.base)
            return
        raise RuntimeError("로그인 rate limit — 잠시 후 다시 시도하세요.")

    # --- 조회 ---

    def _get(self, path: str, **params) -> dict:
        for attempt in range(MAX_RETRIES):
            r = self.session.get(f"{self.base}{path}", params=params, timeout=60)
            if r.status_code == 429:
                wait = 2 ** attempt * 5
                log.warning("rate limit, %ds 대기", wait)
                time.sleep(wait)
                continue
            if r.status_code == 401:
                log.info("토큰 만료 — 재인증")
                self._token_path.unlink(missing_ok=True)
                self._authenticate()
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"{path}: {MAX_RETRIES}회 재시도 후에도 실패")

    def count_notes(self, **query) -> int:
        """조건에 맞는 note 총 개수. (limit>=3 + offset이 있어야 count가 온다)"""
        data = self._get("/notes", **query, limit=3, offset=0)
        return data.get("count", 0)

    def iter_notes(self, **query):
        """페이지네이션을 처리하며 note를 하나씩 yield."""
        offset = 0
        while True:
            data = self._get("/notes", **query, limit=PAGE_SIZE, offset=offset)
            notes = data.get("notes", [])
            yield from notes
            offset += len(notes)
            if len(notes) < PAGE_SIZE:
                return

    def get_forum_replies(self, forum_id: str) -> list[dict]:
        """논문 forum의 모든 리플라이 (리뷰/메타리뷰/rebuttal/decision)."""
        return list(self.iter_notes(forum=forum_id))


# --- venue 레지스트리 (실측으로 확인된 API 버전 및 invitation 이름) ---

VENUE_REGISTRY = {
    "ICLR 2020":    (V1, "ICLR.cc/2020/Conference/-/Blind_Submission"),
    "ICLR 2021":    (V1, "ICLR.cc/2021/Conference/-/Blind_Submission"),
    "ICLR 2022":    (V1, "ICLR.cc/2022/Conference/-/Blind_Submission"),
    "ICLR 2023":    (V1, "ICLR.cc/2023/Conference/-/Blind_Submission"),
    "ICLR 2024":    (V2, "ICLR.cc/2024/Conference/-/Submission"),
    "ICLR 2025":    (V2, "ICLR.cc/2025/Conference/-/Submission"),
    "NeurIPS 2021": (V1, "NeurIPS.cc/2021/Conference/-/Blind_Submission"),
    "NeurIPS 2022": (V1, "NeurIPS.cc/2022/Conference/-/Blind_Submission"),
    "NeurIPS 2023": (V2, "NeurIPS.cc/2023/Conference/-/Submission"),
    "NeurIPS 2024": (V2, "NeurIPS.cc/2024/Conference/-/Submission"),
}


_clients: dict[str, OpenReviewClient] = {}


def get_client(base: str) -> OpenReviewClient:
    """API 버전별 클라이언트를 캐시해서 재사용 (재로그인 방지)."""
    if base not in _clients:
        _clients[base] = OpenReviewClient(base)
    return _clients[base]
