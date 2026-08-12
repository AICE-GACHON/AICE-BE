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

from paper_assistant import config
from paper_assistant.ingest._http import new_session, request_with_retry

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
        self.session = new_session()
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

    @staticmethod
    def _login_decide(r, attempt: int) -> float | None:
        if r.status_code != 429:
            return None
        wait = 2 ** attempt * 10
        log.warning("로그인 rate limit, %ds 대기", wait)
        return wait

    def _authenticate(self) -> None:
        token = self._cached_token()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
            log.debug("캐시된 토큰 사용 (%s)", self.base)
            return

        r = request_with_retry(
            self.session, "POST", f"{self.base}/login", decide=self._login_decide,
            retries=MAX_RETRIES, timeout=30, label="/login",
            json={"id": self.username, "password": self.password})
        token = r.json()["token"]
        self.session.headers["Authorization"] = f"Bearer {token}"
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(json.dumps({"token": token}))
        log.info("OpenReview 로그인 성공: %s (%s)", self.username, self.base)

    # --- 조회 ---

    def _decide(self, r, attempt: int) -> float | None:
        if r.status_code == 429:
            wait = 2 ** attempt * 5
            log.warning("rate limit, %ds 대기", wait)
            return wait
        if r.status_code == 401:
            # 토큰이 만료됐다. 캐시를 버리고 재발급받은 뒤 곧바로 다시 시도한다.
            log.info("토큰 만료 — 재인증")
            self._token_path.unlink(missing_ok=True)
            self._authenticate()
            return 0
        return None

    def _get(self, path: str, **params) -> dict:
        r = request_with_retry(self.session, "GET", f"{self.base}{path}",
                               decide=self._decide, retries=MAX_RETRIES,
                               timeout=60, label=path, params=params)
        return r.json()

    def get_bytes(self, url: str) -> bytes:
        """완성된 URL을 그대로 GET해 바이트를 반환한다 (JSON이 아닌 첨부파일용).

        _get과 달리 self.base를 앞에 붙이지 않는다 — url은 이미 완성된 주소
        (revisions.py의 before_url/after_url 등)를 그대로 받는다. 404 등은
        request_with_retry가 재시도 없이 즉시 raise_for_status로 예외를 던지므로,
        호출부에서 URL 단위로 실패를 감싸야 한다.
        """
        r = request_with_retry(self.session, "GET", url, decide=self._decide,
                               retries=MAX_RETRIES, timeout=60, label="attachment")
        return r.content

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

    def get_note_edits(self, note_id: str) -> list[dict]:
        """note의 수정 이력을 시간순(오래된 것부터)으로.

        **v2 전용**이다. v1에는 /references가 있지만 공개로 읽히는 건 code 링크·
        게재일 같은 운영 메타데이터뿐이고 저자가 고친 제목·초록·PDF는 안 보인다
        (v1 venue 5곳 30편 실측: 본문 리비전 0건). 그래서 v1은 호출하지 않는다.

        주의: 각 edit의 note.content는 전체 스냅샷이 아니라 **그 시점에 바뀐
        필드만** 담은 부분 패치다. 값은 {"value": ...}로 감싸여 있고, 필드 삭제는
        {"delete": True}로 온다. 버전을 복원하려면 앞에서부터 누적 적용해야 한다.
        """
        if self.base != V2:
            return []
        data = self._get("/notes/edits", **{"note.id": note_id})
        return sorted(data.get("edits", []), key=lambda e: e.get("tcdate", 0))


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
