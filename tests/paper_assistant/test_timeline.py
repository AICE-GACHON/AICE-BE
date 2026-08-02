"""타임라인 병합의 순수 로직 테스트 (DB·네트워크 불필요).

fixture는 전부 OpenReview 실측 형태다 — 서명이
'ICLR.cc/2024/Conference/Submission9141/Authors' 꼴이고, v2는 invitations(복수)
v1은 invitation(단수)이며, 제출 노트 자신이 replies에 섞여 온다.
"""
from paper_assistant.query.timeline import _actor, build_timeline
from paper_assistant.schemas import FieldChange, ReviewDetail, RevisionEntry

BASE = "ICLR.cc/2024/Conference"


def note(note_id, kind, signer, tcdate, tmdate=None, content=None):
    return {
        "id": note_id,
        "invitations": [f"{BASE}/Submission1/-/{kind}", f"{BASE}/-/Edit"],
        "signatures": [f"{BASE}/Submission1/{signer}"],
        "tcdate": tcdate, "tmdate": tmdate or tcdate,
        "content": content or {},
    }


def review_detail(rating=6.0, raw="6: marginally above"):
    return ReviewDetail(rating=rating, rating_raw=raw)


# ---------------------------------------------------------------- _actor

def test_actor_reads_role_from_signature_tail():
    assert _actor(note("x", "Official_Comment", "Authors", 1))[0] == "author"
    assert _actor(note("x", "Official_Review", "Reviewer_AHeX", 1)) == (
        "reviewer", "리뷰어 AHeX")
    assert _actor(note("x", "Meta_Review", "Area_Chair_RTU9", 1))[0] == "ac"


def test_actor_falls_back_when_unsigned():
    assert _actor({"signatures": []}) == ("other", "익명")


# --------------------------------------------------------- build_timeline

def test_review_body_comes_from_db_not_from_the_live_note():
    # 라이브 노트에도 본문이 있지만, venue별 필드명 차이를 흡수한 결과는 수집
    # 시점에 이미 계산돼 있다. DB 쪽을 써야 두 화면의 리뷰가 어긋나지 않는다.
    replies = [note("rev1", "Official_Review", "Reviewer_AHeX", 1_000)]
    events = build_timeline(replies, [], {"rev1": review_detail(raw="8: accept")})
    assert len(events) == 1
    assert events[0].kind == "review"
    assert events[0].review.rating_raw == "8: accept"
    assert "8: accept" in events[0].headline


def test_review_missing_from_db_is_parsed_from_the_live_note():
    replies = [note("rev1", "Official_Review", "Reviewer_AHeX", 1_000,
                    content={"rating": {"value": "5: marginally below"},
                             "weaknesses": {"value": "no baselines"}})]
    events = build_timeline(replies, [], {})
    assert events[0].review.rating == 5.0
    assert events[0].review.weaknesses == "no baselines"


def test_edited_review_adds_an_update_event_without_inventing_a_before_score():
    # 수정 전 점수는 복원할 수 없다(리뷰 edit의 첫 항목은 content가 비어 온다).
    # 그래서 '올랐다/내렸다'가 아니라 '수정됐다'만 말해야 한다.
    replies = [note("rev1", "Official_Review", "Reviewer_AHeX",
                    1_000, tmdate=9_000)]
    events = build_timeline(replies, [], {"rev1": review_detail()})
    update = [e for e in events if e.kind == "review_update"]
    assert len(update) == 1
    assert update[0].at == 9_000
    assert update[0].rating == 6.0
    assert "공개되지 않습니다" in update[0].headline
    # before를 담을 자리가 아예 없어야 한다
    assert not hasattr(update[0], "rating_before")


def test_unedited_review_has_no_update_event():
    replies = [note("rev1", "Official_Review", "Reviewer_AHeX", 1_000)]
    events = build_timeline(replies, [], {"rev1": review_detail()})
    assert [e.kind for e in events] == ["review"]


def test_author_comment_is_rebuttal_and_reviewer_comment_is_not():
    replies = [
        note("c1", "Official_Comment", "Authors", 2_000,
             content={"title": {"value": "Response"},
                      "comment": {"value": "we added a table"}}),
        note("c2", "Official_Comment", "Reviewer_KRjg", 3_000,
             content={"comment": {"value": "thanks"}}),
    ]
    events = build_timeline(replies, [], {})
    assert [e.kind for e in events] == ["rebuttal", "comment"]
    assert events[0].text == "we added a table"
    assert events[0].actor == "저자"


def test_meta_review_and_decision_are_separate_kinds():
    replies = [
        note("m1", "Meta_Review", "Area_Chair_RTU9", 4_000,
             content={"metareview": {"value": "solid paper"}}),
        note("d1", "Decision", "Program_Chairs", 5_000,
             content={"decision": {"value": "Accept (poster)"}}),
    ]
    events = build_timeline(replies, [], {})
    assert [e.kind for e in events] == ["meta_review", "decision"]
    assert "Accept (poster)" in events[1].headline


def test_submission_note_itself_is_dropped():
    # replies에는 제출 노트가 섞여 온다(실측). 그 수정 이력은 revisions가 담으므로
    # 여기서 또 이벤트로 만들면 같은 사건이 두 번 나온다.
    replies = [
        {"id": "sub1", "invitations": [f"{BASE}/-/Submission",
                                       f"{BASE}/-/Rebuttal_Revision"],
         "signatures": [f"{BASE}/Submission1/Authors"], "tcdate": 1},
        note("rev1", "Official_Review", "Reviewer_AHeX", 1_000),
    ]
    events = build_timeline(replies, [], {}, submission_id="sub1")
    assert [e.kind for e in events] == ["review"]


def test_v1_single_invitation_field_is_understood():
    # 2023년 이전 venue는 invitation(단수)로 온다. 이걸 놓치면 구 학회 타임라인이
    # 통째로 비어버린다 — 정작 리뷰·코멘트는 열려 있는데도.
    replies = [{
        "id": "rev1", "invitation": "ICLR.cc/2022/Conference/-/Official_Review",
        "signatures": ["ICLR.cc/2022/Conference/Paper1/AnonReviewer1"],
        "tcdate": 1_000, "content": {"recommendation": "8: Top 50%"},
    }]
    events = build_timeline(replies, [], {})
    assert [e.kind for e in events] == ["review"]


def test_events_are_sorted_by_time_with_review_before_reply_on_ties():
    replies = [
        note("c1", "Official_Comment", "Authors", 1_000),
        note("rev1", "Official_Review", "Reviewer_AHeX", 1_000),
    ]
    events = build_timeline(replies, [], {"rev1": review_detail()})
    assert [e.kind for e in events] == ["review", "rebuttal"]


def test_revisions_join_the_same_axis():
    replies = [note("rev1", "Official_Review", "Reviewer_AHeX", 2_000)]
    revisions = [
        RevisionEntry(revision_id="e0", kind="submission", kind_label="최초 제출",
                      timestamp=1_000, date="2024-01-01 00:00", is_baseline=True),
        RevisionEntry(revision_id="e1", kind="rebuttal",
                      kind_label="리뷰 반영 수정", timestamp=3_000,
                      date="2024-01-03 00:00",
                      changes=[FieldChange(field="abstract", label="초록",
                                           kind="text", similarity=0.8)]),
    ]
    events = build_timeline(replies, revisions, {"rev1": review_detail()})
    assert [e.kind for e in events] == [
        "author_revision", "review", "author_revision"]
    assert events[0].is_baseline
    assert "초록" in events[2].headline


def test_revision_without_tracked_changes_does_not_claim_nothing_changed():
    # 우리가 추적하는 필드 밖에서만 바뀐 경우다. '변경 없음'이라고 쓰면
    # 사실보다 강한 주장이 된다.
    revisions = [RevisionEntry(revision_id="e1", kind="revision",
                               kind_label="수정본", timestamp=1_000,
                               date="2024-01-01 00:00")]
    events = build_timeline([], revisions, {})
    assert "제목·초록·첨부파일에는 변화가 없습니다" in events[0].headline
