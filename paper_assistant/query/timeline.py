"""심사 타임라인 조립 — 리뷰·저자 응답·수정본을 한 시간축에 올린다.

`get_paper_reviews`는 리뷰를 점수순으로 줄 뿐이고 `get_paper_revisions`는 저자가
고친 것만 준다. 둘 사이에 시간 관계가 없어서 "무엇을 지적받고 무엇을 고쳤는지"가
화면에서 이어지지 않았다. 여기서 그 둘을 붙인다.

**리뷰 시각은 DB에 없다.** reviews 테이블에 cdate 컬럼이 없다(원본 JSONB를 담으려던
raw_content도 한 번도 채워진 적이 없어 alembic 0012에서 지웠다). 시각을 얻으려면
OpenReview forum replies를 실시간 조회하는 수밖에 없다. 그래서 이 모듈은 네트워크를
탄다 — 리뷰 본문·점수는 DB에서
가져오고 라이브 응답에서는 **시각과 저자 응답만** 취한다. 파싱 규칙(venue마다 다른
필드명)을 두 번 구현하지 않기 위해서다.

v1 venue(2023년 이전)도 forum replies는 열려 있다(ICLR 2022 실측: 리뷰 4건 +
Official_Comment 8~11건). 수정 이력(edits)만 v2 전용이므로, 구 venue는 diff 없이
리뷰·응답 타임라인까지는 제공한다 — 기존 /revisions가 supported=false로 아무것도
주지 못하던 구간이다.

조립 자체는 순수 함수(build_timeline)로 두어 네트워크 없이 테스트한다.
"""
import logging
from datetime import datetime

from paper_assistant.ingest.normalize import (
    METAREVIEW_FIELDS, get_field, normalize_review)
from paper_assistant.query.revisions import KST
from paper_assistant.schemas import ReviewDetail, RevisionEntry, TimelineEvent

log = logging.getLogger(__name__)

# 코멘트 원문 상한. 한 스레드에 18건까지 달리고(실측) 전문이 수천 자라
# 응답과 캐시가 통째로 커진다. 자른 건 프론트가 OpenReview 링크로 보낼 수 있다.
MAX_TEXT = 3000

# 같은 밀리초에 걸린 이벤트의 표시 순서. 리뷰가 먼저 오고 응답이 뒤에 와야
# 인과가 뒤집혀 보이지 않는다.
_KIND_ORDER = {
    "review": 0, "review_update": 1, "rebuttal": 2, "comment": 3,
    "author_revision": 4, "meta_review": 5, "decision": 6,
}

_KIND_LABEL = {
    "review": "리뷰", "review_update": "리뷰 수정", "rebuttal": "저자 응답",
    "comment": "코멘트", "author_revision": "저자 수정", "meta_review": "AC 총평",
    "decision": "최종 결과",
}


def _invitation_kinds(note: dict) -> list[str]:
    """v2는 invitations(복수), v1은 invitation(단수). 접미사만 돌려준다.

    normalize._invitation_kinds와 같은 규칙이지만 그쪽은 비공개 헬퍼라 여기서
    다시 쓴다 — 수집기 내부 구현에 조회 경로를 묶지 않기 위해서다.
    """
    invs = note.get("invitations") or (
        [note["invitation"]] if note.get("invitation") else [])
    return [i.split("/")[-1] for i in invs]


def _actor(note: dict) -> tuple[str, str]:
    """서명에서 (역할, 표시 이름)을 뽑는다.

    서명은 'ICLR.cc/2024/Conference/Submission9141/Authors' 형태라 마지막
    조각만 보면 된다 (실측: Authors / Reviewer_AHeX / Area_Chair_RTU9 /
    Program_Chairs).
    """
    sigs = note.get("signatures") or []
    tail = sigs[0].split("/")[-1] if sigs else ""
    if tail == "Authors":
        return "author", "저자"
    if tail.startswith("Reviewer_"):
        return "reviewer", f"리뷰어 {tail.removeprefix('Reviewer_')}"
    if tail.startswith("Area_Chair"):
        return "ac", "AC"
    if tail.startswith("Program_Chair"):
        return "pc", "PC"
    return "other", tail.replace("_", " ") or "익명"


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT].rstrip() + " …"


def _at(note: dict) -> int:
    return note.get("tcdate") or note.get("cdate") or 0


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, KST).strftime("%Y-%m-%d %H:%M")


def _event(kind: str, event_id: str, at: int, actor: str, headline: str,
           **extra) -> TimelineEvent:
    return TimelineEvent(
        event_id=event_id, at=at, date=_date(at), kind=kind,
        kind_label=_KIND_LABEL[kind], actor=actor, headline=headline, **extra)


def _review_event(note: dict, db_reviews: dict[str, ReviewDetail]) -> TimelineEvent:
    note_id = note.get("id", "")
    _, actor = _actor(note)

    detail = db_reviews.get(note_id)
    if detail is None:
        # 코퍼스 적재 이후에 달린 리뷰이거나 수집에서 빠진 건. 라이브 노트를
        # 수집기와 같은 규칙으로 파싱해 채운다 — venue별 필드명 차이를 여기서
        # 다시 구현하지 않기 위해 normalize_review를 그대로 쓴다.
        nr = normalize_review(note)
        detail = ReviewDetail(
            rating=nr.rating, rating_raw=nr.rating_raw or None,
            confidence=nr.confidence, summary=nr.summary or None,
            strengths=nr.strengths or None, weaknesses=nr.weaknesses or None,
            questions=nr.questions or None, is_unsplit=nr.needs_llm_split)

    score = detail.rating_raw or (
        str(detail.rating) if detail.rating is not None else "점수 미기재")
    return _event("review", note_id, _at(note), actor,
                  f"{actor} — {score}", review=detail)


def _update_event(note: dict, detail: ReviewDetail | None) -> TimelineEvent:
    """리뷰가 최초 게시 이후 수정된 사실만 세운다.

    수정 **전** 점수는 복원할 수 없다 — 리뷰 노트의 edit 이력에서 첫 edit은
    content가 빈 채로 내려오고 마지막 edit에만 전체 내용이 실린다(실측 3건 전부).
    그래서 'n점 → m점'을 만들지 않고, 최종 점수와 '이전 값은 비공개'만 말한다.
    """
    _, actor = _actor(note)
    rating = detail.rating if detail else None
    tail = f" (최종 {detail.rating_raw})" if detail and detail.rating_raw else ""
    return _event("review_update", f"{note.get('id', '')}:update",
                  note.get("tmdate", 0), actor,
                  f"{actor}가 저자 응답 이후 리뷰를 수정했습니다{tail}"
                  " — 수정 전 내용은 공개되지 않습니다",
                  rating=rating)


def _comment_event(note: dict) -> TimelineEvent:
    role, actor = _actor(note)
    content = note.get("content") or {}
    body = get_field(content, ["comment", "rebuttal", "response"])
    title = get_field(content, ["title"])
    kind = "rebuttal" if role == "author" else "comment"
    return _event(kind, note.get("id", ""), _at(note), actor,
                  f"{actor}: {title}" if title else f"{actor}의 코멘트",
                  text=_clip(body) or None)


def _meta_event(note: dict, kinds: list[str]) -> TimelineEvent:
    content = note.get("content") or {}
    _, actor = _actor(note)
    if "Decision" in kinds:
        # Meta_Review 노트가 없는 venue는 총평이 Decision 안에 들어 있다
        # (normalize.normalize_paper의 decision_meta와 같은 사정).
        verdict = get_field(content, ["decision", "recommendation"]) or "결과 공개"
        body = get_field(content, METAREVIEW_FIELDS)
        return _event("decision", note.get("id", ""), _at(note), actor,
                      f"최종 결과: {verdict}", text=_clip(body) or None)
    return _event("meta_review", note.get("id", ""), _at(note), actor,
                  "AC 총평", text=_clip(get_field(content, METAREVIEW_FIELDS)) or None)


def _revision_event(rev: RevisionEntry) -> TimelineEvent:
    if rev.is_baseline:
        headline = "관측 가능한 최초 제출본 — 이전 내용은 확인할 수 없습니다"
    elif rev.changes:
        headline = f"{rev.kind_label} — " + ", ".join(
            c.label for c in rev.changes[:4])
    else:
        # 우리가 추적하는 필드(제목·초록·PDF 등) 밖에서만 바뀐 경우.
        # '변경 없음'이 아니라 '보이는 범위에 변경 없음'이다.
        headline = f"{rev.kind_label} — 제목·초록·첨부파일에는 변화가 없습니다"
    return _event("author_revision", rev.revision_id, rev.timestamp, "저자",
                  headline, changes=rev.changes, is_baseline=rev.is_baseline)


def build_timeline(replies: list[dict], revisions: list[RevisionEntry],
                   db_reviews: dict[str, ReviewDetail],
                   submission_id: str = "") -> list[TimelineEvent]:
    """forum replies + 리비전을 시간순 이벤트 목록으로 병합한다.

    순수 함수다 — replies는 이미 받아온 dict 목록이고 DB도 네트워크도 타지 않는다.

    submission_id: 제출 노트 자신의 id. replies에 자기 자신이 섞여 오므로
    (실측: invitations에 Submission/Post_Submission이 붙은 노트 1건) 걸러낸다.
    그 노트의 수정 이력은 revisions가 이미 담고 있다.
    """
    events: list[TimelineEvent] = []

    for note in replies:
        kinds = _invitation_kinds(note)
        note_id = note.get("id", "")
        if note_id == submission_id or "Submission" in kinds or \
                "Blind_Submission" in kinds:
            continue

        if "Official_Review" in kinds:
            events.append(_review_event(note, db_reviews))
            # tmdate > tcdate면 최초 게시 이후 손을 댔다는 뜻. 추가 API 호출 없이
            # reply에 실려 오는 값만으로 판정한다.
            if note.get("tmdate", 0) > _at(note):
                events.append(_update_event(note, db_reviews.get(note_id)))
        elif "Meta_Review" in kinds or "Decision" in kinds:
            events.append(_meta_event(note, kinds))
        elif any(k.endswith("Comment") or "Rebuttal" in k for k in kinds):
            events.append(_comment_event(note))

    events.extend(_revision_event(r) for r in revisions)
    events.sort(key=lambda e: (e.at, _KIND_ORDER.get(e.kind, 9)))
    return events
