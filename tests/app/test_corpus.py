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


def test_story_refresh_flag_is_passed_through(client, stub_story):
    client.get("/api/papers/27030/story?refresh=true")
    assert stub_story[0]["refresh"] is True
