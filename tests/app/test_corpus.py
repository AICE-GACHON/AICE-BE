"""코퍼스 조회 라우터 테스트 — 특히 심사 서사(/story).

paper_assistant 쪽은 monkeypatch로 갈아끼운다. 실제 함수는 OpenReview API를 두 번
타고 LLM까지 부르므로 라우터 계약(상태 코드·응답 모양·쿼리 파라미터 전달)만
여기서 검증하고, 조립 로직은 tests/paper_assistant/test_timeline.py 등이 맡는다.
"""
import pytest

from app.routers import corpus as corpus_router
from paper_assistant.schemas import (
    JourneyStop, PaperStory, StoryNarrative, SubmissionJourney, TimelineEvent)


def make_story(paper_id: int = 27030) -> PaperStory:
    return PaperStory(
        paper_id=paper_id, openreview_id="EzjsoomYEb", title="A paper",
        venue="ICLR 2025", year=2025, decision="accept-oral",
        journey=SubmissionJourney(
            stops=[JourneyStop(paper_id=paper_id, openreview_id="EzjsoomYEb",
                               title="A paper", venue="ICLR 2025", year=2025,
                               decision="accept-oral", is_query=True)],
            outcome="single"),
        timeline=[TimelineEvent(
            event_id="rev1", at=1_000, date="2024-11-22 03:42", kind="review",
            kind_label="리뷰", actor="리뷰어 AHeX", headline="리뷰어 AHeX — 6")],
        timeline_supported=True,
        narrative=StoryNarrative(headline="요약", evidence_scope="abstract_only"),
        caveats=["리뷰 본문과 점수는 최종 수정본입니다."])


@pytest.fixture
def stub_story(monkeypatch):
    """호출 인자를 기록하면서 고정된 PaperStory를 돌려준다."""
    calls: list[dict] = []

    def _fake(paper_id, use_llm=None, refresh=False):
        calls.append({"paper_id": paper_id, "refresh": refresh})
        return None if paper_id == 999_999 else make_story(paper_id)

    monkeypatch.setattr(corpus_router, "get_paper_story", _fake)
    return calls


@pytest.fixture
def stub_revisions(monkeypatch):
    """`/revisions`도 OpenReview를 탄다 — rate limit 테스트에서 실제 호출을 막는다."""
    from paper_assistant.schemas import PaperRevisions

    monkeypatch.setattr(
        corpus_router, "get_paper_revisions",
        lambda paper_id: PaperRevisions(
            paper_id=paper_id, openreview_id="EzjsoomYEb", supported=True))


@pytest.fixture
def stub_revisions_body(monkeypatch):
    """`/revisions/body-diff`도 OpenReview(+PDF 다운로드)를 탄다 — rate limit 테스트에서
    실제 호출을 막는다."""
    from paper_assistant.schemas import PaperRevisions

    calls: list[dict] = []

    def _fake(paper_id, refresh=False):
        calls.append({"paper_id": paper_id, "refresh": refresh})
        return None if paper_id == 999_999 else PaperRevisions(
            paper_id=paper_id, openreview_id="EzjsoomYEb", supported=True)

    monkeypatch.setattr(corpus_router, "get_paper_revisions_with_body", _fake)
    return calls


@pytest.fixture
def stub_detail(monkeypatch):
    """DB만 보는 조회. 상한이 여기까지 번지지 않는지 확인할 때 쓴다."""
    from paper_assistant.schemas import PaperDetail

    monkeypatch.setattr(
        corpus_router, "get_paper_detail",
        lambda paper_id: PaperDetail(
            paper_id=paper_id, openreview_id="EzjsoomYEb", title="A paper",
            venue="ICLR 2025", year=2025, decision="accept-oral",
            openreview_url="https://openreview.net/forum?id=EzjsoomYEb",
            pdf_url="https://openreview.net/pdf?id=EzjsoomYEb"))


def test_story_returns_all_three_parts(client, stub_story):
    res = client.get("/api/papers/27030/story")
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["journey"]["outcome"] == "single"
    assert len(data["timeline"]) == 1
    assert data["narrative"]["headline"] == "요약"
    assert data["caveats"]


def test_story_exposes_the_evidence_limits_to_the_frontend(client, stub_story):
    """timeline_supported와 evidence_scope는 프론트가 경고를 띄우는 근거라
    응답에서 빠지면 안 된다."""
    data = client.get("/api/papers/27030/story").json()["data"]
    assert data["timeline_supported"] is True
    assert data["narrative"]["evidence_scope"] == "abstract_only"
    assert data["narrative"]["used_llm"] is False


def test_story_404_for_unknown_paper(client, stub_story):
    assert client.get("/api/papers/999999/story").status_code == 404


def test_story_defaults_to_the_cache(client, stub_story):
    client.get("/api/papers/27030/story")
    assert stub_story[0]["refresh"] is False


def test_anonymous_refresh_is_rejected(client, stub_story):
    """`refresh=true`는 캐시를 우회하므로 같은 논문에 반복하면 시간당 상한을 무의미하게
    만든다 — 호출마다 OpenReview 2콜 + LLM 1콜이 나간다.

    조용히 캐시를 돌려주지 않고 **401로 거절한다.** 조용히 주면 호출자는 방금 다시
    만든 결과를 받았다고 오해한다.
    """
    res = client.get("/api/papers/27030/story?refresh=true")
    assert res.status_code == 401
    assert stub_story == [], "거절했는데 비싼 함수를 불렀다"


def test_logged_in_refresh_is_passed_through(client, auth, stub_story):
    """회원에게는 여전히 허용한다 — 캐시가 낡았을 때 다시 만들 길은 있어야 한다."""
    res = client.get("/api/papers/27030/story?refresh=true",
                     headers=auth["headers"])
    assert res.status_code == 200
    assert stub_story[0]["refresh"] is True


def test_expired_token_is_treated_as_anonymous(client, stub_story):
    """만료·위조 토큰으로 refresh를 우회할 수 없어야 한다."""
    res = client.get("/api/papers/27030/story?refresh=true",
                     headers={"Authorization": "Bearer not-a-real-token"})
    assert res.status_code == 401
    assert stub_story == []


def test_reading_the_story_needs_no_login(client, stub_story):
    """랜딩의 데모가 비로그인으로 부른다 — 조회 자체는 공개로 남는다."""
    assert client.get("/api/papers/27030/story").status_code == 200


# ------------------------------------------------------------ rate limit
#
# conftest가 테스트 내내 limiter를 꺼 둔다(반복 호출이 429를 맞지 않도록). 그래서
# **여기서만 다시 켜서** 상한이 실제로 걸리는지 본다. 이 테스트가 없으면 데코레이터가
# 사라져도 나머지 테스트는 전부 통과한다 — 비용이 새는 것을 아무도 모르게 된다.

@pytest.fixture
def rate_limit_on():
    from app.core.rate_limit import limiter

    limiter.enabled = True
    limiter.reset()          # 앞선 테스트가 남긴 카운터를 지운다
    try:
        yield
    finally:
        limiter.reset()      # 뒤 테스트에 카운터를 넘기지 않는다
        limiter.enabled = False


@pytest.mark.parametrize("path", ["story", "revisions"])
def test_external_call_endpoints_are_rate_limited(client, stub_story, stub_revisions,
                                                  rate_limit_on, path):
    """캐시에 없는 논문을 연달아 훑는 것을 끊는다.

    두 엔드포인트만 외부 자원(OpenReview 2콜, /story는 LLM까지)을 쓴다. 캐시된 논문을
    다시 읽는 것은 비용이 없으므로 막을 대상이 아니고, 막아야 할 것은 스캔이다.
    """
    from app.routers.corpus import _EXTERNAL_CALL_LIMIT

    allowed = int(_EXTERNAL_CALL_LIMIT.split("/")[0])
    codes = [client.get(f"/api/papers/27030/{path}").status_code
             for _ in range(allowed + 1)]
    assert codes[:allowed] == [200] * allowed
    assert codes[-1] == 429, f"{path}: 상한을 넘겼는데 통과했다"


def test_cheap_endpoints_are_not_rate_limited(client, stub_detail, rate_limit_on):
    """DB만 보는 조회까지 묶으면 목록·상세를 넘기는 정상 사용이 막힌다."""
    from app.routers.corpus import _EXTERNAL_CALL_LIMIT

    n = int(_EXTERNAL_CALL_LIMIT.split("/")[0]) + 5
    codes = {client.get("/api/papers/27030").status_code for _ in range(n)}
    assert codes == {200}


# ------------------------------------------------------ /revisions/body-diff
#
# `/revisions`보다 훨씬 비싼 경로(pdf 교체마다 실제 다운로드+파싱)라 별도 rate limit과
# 별도 라우터 계약을 갖는다. 조립 로직(attach_body_diffs 자체)은
# tests/paper_assistant/test_revisions.py가 맡고, 여기는 라우터 계약만 본다.

def test_revisions_body_diff_404_for_unknown_paper(client, stub_revisions_body):
    assert client.get("/api/papers/999999/revisions/body-diff").status_code == 404


def test_revisions_body_diff_defaults_to_the_cache(client, stub_revisions_body):
    client.get("/api/papers/27030/revisions/body-diff")
    assert stub_revisions_body[0]["refresh"] is False


def test_revisions_body_diff_anonymous_refresh_is_rejected(client, stub_revisions_body):
    """`/story`와 같은 이유 — refresh=true는 캐시를 우회하므로 비로그인이면 거절한다."""
    res = client.get("/api/papers/27030/revisions/body-diff?refresh=true")
    assert res.status_code == 401
    assert stub_revisions_body == [], "거절했는데 비싼 함수를 불렀다"


def test_revisions_body_diff_logged_in_refresh_is_passed_through(
        client, auth, stub_revisions_body):
    res = client.get("/api/papers/27030/revisions/body-diff?refresh=true",
                     headers=auth["headers"])
    assert res.status_code == 200
    assert stub_revisions_body[0]["refresh"] is True


def test_revisions_body_diff_is_rate_limited(client, stub_revisions_body, rate_limit_on):
    """`/revisions`(30/hour)보다 엄격한 자체 상한(10/hour)이 걸려 있어야 한다."""
    from app.routers.corpus import _BODY_DIFF_CALL_LIMIT

    allowed = int(_BODY_DIFF_CALL_LIMIT.split("/")[0])
    codes = [client.get("/api/papers/27030/revisions/body-diff").status_code
             for _ in range(allowed + 1)]
    assert codes[:allowed] == [200] * allowed
    assert codes[-1] == 429, "상한을 넘겼는데 통과했다"
