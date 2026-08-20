"""OpenReview 클라이언트 로직 테스트 (실제 API 호출 없음 — 세션을 가짜로 바꿔 검증).

`test_s2_client.py` 와 같은 방식이다. 세 수집 클라이언트가 `_http.request_with_retry`
뼈대를 공유하지만 **재시도 조건이 서로 달라서**(arXiv는 503+Retry-After, S2는 429와
5xx, OpenReview는 429와 401) 각자 따로 검증해야 한다.

⚠️ **이 파일이 덮지 못하는 것 — 파일 맨 아래 「미커버 영역」 참고.**
타임아웃/연결 끊김은 재시도되지 않으며, 그 사실을 숨기지 않으려고 현재 동작을
그대로 고정하는 테스트를 남겨 두었다.
"""
import json

import pytest
import requests

from paper_assistant.ingest import _http, openreview_client
from paper_assistant.ingest.openreview_client import V1, V2, OpenReviewClient


class FakeResponse:
    def __init__(self, status, payload=None, content=b""):
        self.status_code = status
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"raise_for_status 호출됨: {self.status_code}")


class FakeSession:
    """미리 정해둔 응답을 순서대로 돌려주고 요청을 기록한다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"준비된 응답보다 요청이 많다: {method} {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """재시도 대기는 `_http` 안에서 일어난다 (클라이언트 모듈이 아니라)."""
    monkeypatch.setattr(_http.time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def token_file(tmp_path, monkeypatch):
    """토큰 캐시가 실제 DATA_DIR을 건드리지 않게 한다."""
    path = tmp_path / ".token_v2.json"
    monkeypatch.setattr(
        OpenReviewClient, "_token_path", property(lambda self: path))
    return path


@pytest.fixture
def cached_token(monkeypatch):
    """로그인을 타지 않도록 유효한 캐시 토큰이 있는 것으로 만든다."""
    monkeypatch.setattr(OpenReviewClient, "_cached_token", lambda self: "tok")


def _client(responses, base=V2):
    c = OpenReviewClient(base=base, username="u", password="p")
    c.session = FakeSession(responses)
    return c


# --------------------------------------------------------------- 자격 증명

def test_missing_credentials_raises(monkeypatch):
    monkeypatch.setattr(openreview_client.config, "OPENREVIEW_USERNAME", "")
    monkeypatch.setattr(openreview_client.config, "OPENREVIEW_PASSWORD", "")
    with pytest.raises(RuntimeError, match="OPENREVIEW_USERNAME"):
        OpenReviewClient(base=V2)


# ------------------------------------------------------------------- 인증

def test_cached_token_skips_login(cached_token, monkeypatch):
    """캐시된 토큰이 살아 있으면 /login 을 아예 부르지 않는다.

    (`_client()` 는 생성 뒤에 세션을 바꿔치기하므로 여기서는 쓰지 않는다 —
    인증이 진짜 세션에 무엇을 남겼는지를 봐야 한다.)
    """
    session = FakeSession([])
    monkeypatch.setattr(openreview_client, "new_session", lambda: session)

    OpenReviewClient(base=V2, username="u", password="p")

    assert session.calls == []            # /login 요청이 나가지 않았다
    assert session.headers["Authorization"] == "Bearer tok"


def test_login_retries_on_rate_limit(monkeypatch, token_file):
    """/login 에는 rate limit이 있다 (모듈 docstring). 429면 물러났다 다시 친다."""
    monkeypatch.setattr(OpenReviewClient, "_cached_token", lambda self: None)
    session = FakeSession([
        FakeResponse(429),
        FakeResponse(200, {"token": "fresh"}),
    ])
    monkeypatch.setattr(openreview_client, "new_session", lambda: session)

    c = OpenReviewClient(base=V2, username="u", password="p")

    assert len(session.calls) == 2
    assert c.session.headers["Authorization"] == "Bearer fresh"
    # 받은 토큰은 다음 실행이 재사용하도록 디스크에 남는다
    assert json.loads(token_file.read_text())["token"] == "fresh"


def test_login_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(OpenReviewClient, "_cached_token", lambda self: None)
    monkeypatch.setattr(openreview_client, "MAX_RETRIES", 3)
    session = FakeSession([FakeResponse(429)] * 3)
    monkeypatch.setattr(openreview_client, "new_session", lambda: session)

    with pytest.raises(RuntimeError, match="/login"):
        OpenReviewClient(base=V2, username="u", password="p")
    assert len(session.calls) == 3


# ------------------------------------------------------------------- 조회

def test_get_retries_on_rate_limit(cached_token):
    c = _client([FakeResponse(429), FakeResponse(200, {"count": 7})])
    assert c.count_notes(invitation="x") == 7
    assert len(c.session.calls) == 2


def test_gives_up_after_max_retries(cached_token, monkeypatch):
    monkeypatch.setattr(openreview_client, "MAX_RETRIES", 3)
    c = _client([FakeResponse(429)] * 3)
    with pytest.raises(RuntimeError, match="/notes"):
        c.count_notes(invitation="x")
    assert len(c.session.calls) == 3


def test_401_triggers_reauthentication(cached_token, token_file):
    """토큰 만료(401)면 캐시를 버리고 재인증한 뒤 **곧바로** 다시 시도한다."""
    token_file.write_text(json.dumps({"token": "stale"}))
    c = _client([FakeResponse(401), FakeResponse(200, {"count": 1})])

    assert c.count_notes(invitation="x") == 1
    assert len(c.session.calls) == 2
    assert not token_file.exists()        # 만료된 캐시는 지워진다


def test_count_notes_asks_for_limit_3_and_offset(cached_token):
    """v2는 limit=1이면 캐시 응답을 주고 count를 생략한다 (모듈 docstring).

    이 테스트가 깨지면 count가 0으로 보이면서 수집이 조용히 멈춘다.
    """
    c = _client([FakeResponse(200, {"count": 42})])
    c.count_notes(invitation="x")

    params = c.session.calls[0][2]["params"]
    assert params["limit"] >= 3
    assert params["offset"] == 0


def test_iter_notes_follows_pagination(cached_token, monkeypatch):
    monkeypatch.setattr(openreview_client, "PAGE_SIZE", 2)
    c = _client([
        FakeResponse(200, {"notes": [{"id": "a"}, {"id": "b"}]}),
        FakeResponse(200, {"notes": [{"id": "c"}]}),
    ])

    assert [n["id"] for n in c.iter_notes(forum="f")] == ["a", "b", "c"]
    assert [call[2]["params"]["offset"] for call in c.session.calls] == [0, 2]


def test_iter_notes_stops_on_empty_page(cached_token, monkeypatch):
    monkeypatch.setattr(openreview_client, "PAGE_SIZE", 2)
    c = _client([FakeResponse(200, {"notes": []})])
    assert list(c.iter_notes(forum="f")) == []
    assert len(c.session.calls) == 1


# --------------------------------------------------------------- note edits

def test_note_edits_are_empty_on_v1(cached_token):
    """v1은 저자가 고친 제목·초록·PDF를 공개하지 않는다 — 호출 자체를 하지 않는다."""
    c = _client([], base=V1)
    assert c.get_note_edits("n1") == []
    assert c.session.calls == []


def test_note_edits_sorted_oldest_first(cached_token):
    c = _client([FakeResponse(200, {"edits": [
        {"id": "e2", "tcdate": 200},
        {"id": "e1", "tcdate": 100},
        {"id": "e3", "tcdate": 300},
    ]})])
    assert [e["id"] for e in c.get_note_edits("n1")] == ["e1", "e2", "e3"]


def test_get_bytes_does_not_prefix_base_url(cached_token):
    """첨부파일은 완성된 URL을 그대로 받는다 — base를 앞에 붙이면 404가 된다."""
    c = _client([FakeResponse(200, content=b"%PDF-")])
    assert c.get_bytes("https://example.test/attachment?id=1") == b"%PDF-"

    _method, url, _kwargs = c.session.calls[0]
    assert url == "https://example.test/attachment?id=1"
    assert not url.startswith(V2)


# ===================================================================
# 미커버 영역 — 아래는 "현재 이렇게 동작한다"를 고정할 뿐, 옳다는 뜻이 아니다.
# ===================================================================

def test_timeout_is_not_retried_and_propagates(cached_token):
    """🔴 **알려진 구멍**: 타임아웃·연결 끊김은 재시도되지 않는다.

    `_http.request_with_retry` 의 재시도 판단(`decide`)은 **응답을 받은 뒤**에만
    돈다. `session.request` 자체가 던지는 예외는 잡히지 않으므로, 네트워크가
    느리거나 끊기면 첫 시도에서 그대로 위로 터진다. 429는 5번 물러나 주면서
    타임아웃은 한 번도 안 봐주는 셈이다.

    영향이 가장 큰 곳은 **요청 경로**다. `GET /api/papers/{id}/revisions` 와
    `/story` 는 OpenReview를 실시간으로 부르는데(app/routers/corpus.py), 이 예외를
    잡지 않아 전역 핸들러가 **500**으로 바꾼다 — 사용자에겐 서버 오류로 보인다.

    고치려면 `_http.request_with_retry` 가 `requests.RequestException` 을 잡아
    재시도해야 하는데, 그 함수는 arXiv·S2 클라이언트도 함께 쓰므로 세 곳의 수집
    동작이 같이 바뀐다. 범위가 커서 **의도적으로 미뤘다.**

    이 테스트가 깨진다면 재시도가 생겼다는 뜻이니, 그때 이 문서를 지우고
    "타임아웃도 재시도된다"는 테스트로 바꿔 달라.
    """
    c = _client([requests.Timeout("연결이 너무 느립니다")])

    with pytest.raises(requests.Timeout):
        c.count_notes(invitation="x")

    assert len(c.session.calls) == 1      # 재시도 없이 한 번에 끝났다
