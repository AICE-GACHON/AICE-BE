"""S2 클라이언트 로직 테스트 (실제 API 호출 없음 — 세션을 가짜로 바꿔 검증)."""
import pytest

from paper_assistant.ingest import s2_client
from paper_assistant.ingest.s2_client import S2Client, arxiv_ref


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

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
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(s2_client.time, "sleep", lambda _s: None)


def _client(responses):
    c = S2Client(api_key="dummy", gap=0.0)
    c.session = FakeSession(responses)
    return c


def test_missing_key_raises():
    with pytest.raises(RuntimeError, match="S2_API_KEY"):
        S2Client(api_key="")


def test_arxiv_ref_strips_version():
    assert arxiv_ref("2401.01234v3") == "ARXIV:2401.01234"
    assert arxiv_ref(" 1409.0473 ") == "ARXIV:1409.0473"
    # 구형 id에 들어 있는 'v'를 버전으로 착각하면 안 된다
    assert arxiv_ref("solv-int/9901001v1") == "ARXIV:solv-int/9901001"


def test_paper_batch_splits_into_chunks(monkeypatch):
    monkeypatch.setattr(s2_client, "BATCH_SIZE", 2)
    c = _client([FakeResponse(200, [{"paperId": "a"}, None]),
                 FakeResponse(200, [{"paperId": "c"}])])
    out = c.paper_batch(["A", "B", "C"], "paperId")
    assert out == [{"paperId": "a"}, None, {"paperId": "c"}]
    assert [call[2]["json"]["ids"] for call in c.session.calls] == [["A", "B"], ["C"]]


def test_paper_batch_detects_length_mismatch():
    c = _client([FakeResponse(200, [{"paperId": "a"}])])
    with pytest.raises(RuntimeError, match="길이 불일치"):
        c.paper_batch(["A", "B"], "paperId")


def test_retries_on_rate_limit_and_server_error():
    c = _client([FakeResponse(429), FakeResponse(500),
                 FakeResponse(200, [{"paperId": "a"}])])
    assert c.paper_batch(["A"], "paperId") == [{"paperId": "a"}]
    assert len(c.session.calls) == 3


def test_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(s2_client, "MAX_RETRIES", 3)
    c = _client([FakeResponse(429)] * 3)
    with pytest.raises(RuntimeError, match="재시도 후에도 실패"):
        c.paper_batch(["A"], "paperId")


def test_search_bulk_follows_token_pagination():
    c = _client([
        FakeResponse(200, {"total": 3, "token": "t1",
                           "data": [{"paperId": "a"}, {"paperId": "b"}]}),
        FakeResponse(200, {"total": 3, "data": [{"paperId": "c"}]}),
    ])
    got = list(c.search_bulk("paperId", venue="ICLR", year="2024"))
    assert [r["paperId"] for r in got] == ["a", "b", "c"]
    # 2번째 요청에는 token이 실려야 한다
    assert c.session.calls[1][2]["params"]["token"] == "t1"


def test_search_bulk_stops_on_empty_page():
    c = _client([FakeResponse(200, {"total": 0, "token": "t1", "data": []})])
    assert list(c.search_bulk("paperId", venue="ICLR")) == []
