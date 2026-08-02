"""서사 요약의 순수 로직 테스트 (LLM·DB·네트워크 불필요).

여기서 지키는 건 대부분 '말하지 않아야 할 것'이다 — 스텁이 근거 없는 인과나
점수 변화를 만들어내지 않고, 분류 실패 버킷('other')을 대표 지적으로 올리지
않는다는 것.
"""
from paper_assistant.query.narrative import (
    _asked_from_points, _changed_from_timeline, _count_revisions, _facts,
    _josa, _stub)
from paper_assistant.schemas import (
    DiffSegment, FieldChange, JourneyStop, SubmissionJourney, TimelineEvent)


def point(aspect, sentiment="weakness", text="t", unsplit=False):
    return (aspect, sentiment, text, unsplit)


def event(kind, **kw):
    kw.setdefault("event_id", "e")
    kw.setdefault("at", 1_000)
    kw.setdefault("date", "2024-11-22 03:42")
    kw.setdefault("kind_label", kind)
    kw.setdefault("actor", "저자")
    kw.setdefault("headline", "h")
    return TimelineEvent(kind=kind, **kw)


def journey(*stops):
    return SubmissionJourney(stops=list(stops))


# ------------------------------------------------------------------ _josa

def test_josa_follows_the_final_consonant():
    assert _josa("8건", "이", "가") == "이"      # ㄴ 받침
    assert _josa("1회", "이", "가") == "가"      # 받침 없음
    assert _josa("", "이", "가") == "가"


# -------------------------------------------------- _asked_from_points

def test_other_never_leads_even_when_it_dominates():
    # 실측: 한 논문에서 기타 12건 vs 중요도 1건. 빈도로 줄 세우면 "이 논문은
    # 기타를 지적받았습니다"가 되어 정보가 0이 된다.
    ranked = _asked_from_points(
        [point("other")] * 12 + [point("significance")])
    assert ranked[0] == ("중요도", 1)
    assert ranked[-1] == ("기타", 12)


def test_strengths_and_questions_are_not_counted_as_criticism():
    ranked = _asked_from_points([
        point("novelty", sentiment="strength"),
        point("clarity", sentiment="question"),
        point("baselines")])
    assert ranked == [("베이스라인 비교", 1)]


def test_unsplit_review_points_are_excluded():
    # 강/약점 미분리 리뷰에서 나온 문장은 지적인지 확정할 수 없다.
    assert _asked_from_points([point("novelty", unsplit=True)]) == []


def test_ties_are_broken_deterministically():
    a = _asked_from_points([point("novelty"), point("clarity")])
    b = _asked_from_points([point("clarity"), point("novelty")])
    assert a == b


# ------------------------------------------------ _changed_from_timeline

def test_text_change_is_reported_as_percent_changed():
    ev = event("author_revision", changes=[
        FieldChange(field="abstract", label="초록", kind="text", similarity=0.75)])
    assert _changed_from_timeline([ev]) == ["2024-11-22 초록 25% 변경"]


def test_baseline_revision_is_not_counted_as_a_change():
    # 관측 가능한 첫 버전은 diff가 없다. 이걸 '수정 1회'로 세면 아무것도 안 고친
    # 논문이 고친 것처럼 보인다.
    ev = event("author_revision", is_baseline=True)
    assert _count_revisions([ev]) == 0
    assert _changed_from_timeline([ev]) == []


def test_rebuttals_are_counted_separately_from_revisions():
    events = [event("rebuttal"), event("rebuttal"),
              event("author_revision", changes=[
                  FieldChange(field="pdf", label="본문 PDF", kind="file",
                              after="교체됨")])]
    assert _changed_from_timeline(events) == [
        "저자 응답 2건", "2024-11-22 본문 PDF 교체됨"]


# ------------------------------------------------------------------ _stub

def test_stub_says_plainly_when_there_is_no_author_response():
    n = _stub("reject", [point("novelty")], [], journey(), "replies_only")
    assert "공개된 저자 대응 기록은 없습니다" in n.headline
    assert n.used_llm is False
    assert n.evidence_scope == "replies_only"


def test_stub_headline_picks_the_correct_particle():
    one = _stub("accept", [point("clarity")],
                [event("author_revision", changes=[
                    FieldChange(field="pdf", label="PDF", kind="file",
                                after="교체됨")])],
                journey(), "abstract_only")
    assert "저자 수정 1회가 있었습니다" in one.headline

    many = _stub("accept", [point("clarity")],
                 [event("rebuttal"), event("rebuttal")],
                 journey(), "abstract_only")
    assert "저자 응답 2건이 있었습니다" in many.headline


def test_stub_reports_no_points_without_inventing_any():
    n = _stub("reject", [], [], journey(), "replies_only")
    assert n.headline == "공개된 리뷰 지적 항목이 없습니다."
    assert n.reviewers_asked == []


def test_stub_prefers_the_resubmission_message_for_the_outcome():
    j = SubmissionJourney(
        stops=[JourneyStop(paper_id=1, openreview_id="a", title="t",
                           venue="ICLR 2024", year=2024, decision="reject"),
               JourneyStop(paper_id=2, openreview_id="b", title="t",
                           venue="NeurIPS 2024", year=2024,
                           decision="accept-poster")],
        outcome="improved", message="ICLR 2024에서 reject 뒤 NeurIPS 2024에 …")
    n = _stub("reject", [], [], j, "replies_only")
    assert n.outcome_note == j.message


# ----------------------------------------------------------------- _facts

def test_facts_send_only_the_changed_words_not_the_whole_abstract():
    # equal 조각까지 넘기면 초록 전문을 두 번 싣는 셈이라 입력만 커진다.
    ev = event("author_revision", changes=[FieldChange(
        field="abstract", label="초록", kind="text", similarity=0.5,
        segments=[DiffSegment(op="equal", text="we propose"),
                  DiffSegment(op="insert", text="on ZINC"),
                  DiffSegment(op="delete", text="on QM9")])])
    facts = _facts("t", "ICLR 2025", "reject", [], [ev], journey())
    field = facts["observed_revisions"][0]["fields"][0]
    assert field["inserted"] == "on ZINC"
    assert field["removed"] == "on QM9"
    assert "we propose" not in str(facts)


def test_facts_flag_review_edits_without_exposing_a_before_score():
    facts = _facts("t", "ICLR 2025", "accept", [],
                   [event("review_update", rating=6.0)], journey())
    assert facts["review_updated_after_reply"] is True
    assert "rating_before" not in str(facts)


def test_facts_omit_resubmission_for_a_single_submission():
    stop = JourneyStop(paper_id=1, openreview_id="a", title="t",
                       venue="ICLR 2024", year=2024, decision="reject")
    assert _facts("t", "ICLR 2024", "reject", [], [],
                  journey(stop))["resubmission"] is None
