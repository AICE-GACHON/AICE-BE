"""심사 서사 조회 — "이 논문은 이런 지적을 받고 이렇게 고쳤다".

네 번째 공개 API다. 세 조각을 하나로 묶는다:

    journey   재투고 궤적            query/journey.py    DB만
    timeline  리뷰·응답·수정 시간축   query/timeline.py   OpenReview 2콜
    narrative 요약                   query/narrative.py  LLM(끄면 스텁)

**세 조각은 서로 독립적으로 실패한다.** 외부 API가 죽어도 journey는 나가고, LLM이
꺼져 있어도 timeline은 나간다. 한 부분이 비었다고 전체를 404로 만들지 않는다 —
사용자에게는 "볼 수 없는 부분"과 "없는 부분"을 구분해 caveats로 알린다.

느리다(외부 2콜 + LLM). 그래서 결과를 paper_stories에 통째로 캐시한다. 심사가 끝난
논문은 리뷰도 수정 이력도 더 바뀌지 않으므로 사실상 영구 캐시다.
"""
import logging
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from paper_assistant import config
from paper_assistant.db.connection import cursor
from paper_assistant.llm import get_llm
from paper_assistant.ingest.openreview_client import V2, VENUE_REGISTRY, get_client
from paper_assistant.query.journey import get_submission_journey
from paper_assistant.query.narrative import build_narrative
# detail.py의 지적항목 쿼리를 그대로 쓴다. 정렬 규칙(미분류 'other'를 뒤로)을
# 여기 복사하면 두 화면의 지적 순서가 조용히 어긋난다.
from paper_assistant.query.detail import _POINTS_SQL
from paper_assistant.query.revisions import build_revisions
from paper_assistant.query.timeline import build_timeline
from paper_assistant.schemas import PaperStory, ReviewDetail, TimelineEvent

log = logging.getLogger(__name__)

# 캐시 유효 기간. 심사가 끝난 논문은 안 바뀌지만, 진행 중인 학회의 논문을 일찍
# 조회했다면 리뷰가 더 붙을 수 있다. 그 경우에만 다시 만들면 되는 값이다.
STALE_AFTER = timedelta(days=30)

_PAPER_SQL = """
    SELECT id, openreview_id, forum_id, title, venue, year, decision
    FROM papers WHERE id = %s
"""

# 리뷰 본문·점수는 DB에서 가져온다. 라이브 노트에도 같은 내용이 있지만, venue별
# 필드명 차이를 흡수한 결과(needs_llm_split 포함)는 수집 시점에 이미 계산돼 있다.
_REVIEWS_SQL = """
    SELECT openreview_id, rating, rating_raw, confidence, summary, strengths,
           weaknesses, questions, needs_llm_split
    FROM reviews WHERE paper_id = %s
"""

_CACHE_GET_SQL = """
    SELECT payload, used_llm, built_at FROM paper_stories WHERE paper_id = %s
"""

_CACHE_PUT_SQL = """
    INSERT INTO paper_stories (paper_id, payload, used_llm, built_at)
    VALUES (%s, %s, %s, now())
    ON CONFLICT (paper_id) DO UPDATE SET
        payload  = EXCLUDED.payload,
        used_llm = EXCLUDED.used_llm,
        built_at = EXCLUDED.built_at
"""

_V1_NOTE = ("이 학회는 저자 수정 이력을 공개하지 않습니다(2023년 이전 학회는 구 "
            "API를 씁니다). 리뷰와 저자 응답은 아래에 있지만, 저자가 제목·초록·PDF를 "
            "어떻게 고쳤는지는 볼 수 없습니다 — 고치지 않았다는 뜻이 아닙니다.")
_BODY_NOTE = ("확인할 수 있는 수정은 제목·초록·첨부파일까지입니다. 논문 본문이 어떻게 "
              "바뀌었는지는 PDF 안에 있어 알 수 없습니다.")
# v2 학회인데도 수정 이력이 하나도 안 열리는 경우가 흔하다 — 무작위 14편 실측에서
# 저자 수정본이 보인 건 3편뿐이고, ICLR 2024·NeurIPS는 대개 게재 확정본만 열린다.
# 이걸 '수정 없음'으로 보여주면 사실과 반대되는 인상을 준다.
_NO_REVISION_NOTE = ("저자가 제목·초록을 고친 기록은 공개되지 않았습니다. 수정하지 "
                     "않았다는 뜻이 아니라, 학회가 수정 내역을 열지 않은 경우입니다.")
_REVIEW_NOTE = ("리뷰 본문과 점수는 최종 수정본입니다. 리뷰어가 저자 응답 이후 리뷰를 "
                "고친 경우 최초 게시 시점의 내용과 다르며, 고치기 전 점수는 "
                "공개되지 않습니다.")


def _db_reviews(cur, paper_id: int) -> dict[str, ReviewDetail]:
    """{리뷰 openreview_id: ReviewDetail}. 라이브 노트와 붙이는 열쇠다."""
    cur.execute(_REVIEWS_SQL, (paper_id,))
    return {
        r[0]: ReviewDetail(
            rating=float(r[1]) if r[1] is not None else None,
            rating_raw=r[2],
            confidence=float(r[3]) if r[3] is not None else None,
            summary=r[4], strengths=r[5], weaknesses=r[6], questions=r[7],
            is_unsplit=bool(r[8]))
        for r in cur.fetchall()
    }


def _fetch_timeline(openreview_id: str, forum_id: str | None, venue: str,
                    db_reviews: dict[str, ReviewDetail],
                    ) -> tuple[list[TimelineEvent], bool, list[str]]:
    """(이벤트, 조회 가능 여부, 주의사항). 외부 API 실패는 예외로 올리지 않는다."""
    base = (VENUE_REGISTRY.get(venue) or (None, None))[0]
    if base is None:
        return [], False, [f"{venue}는 심사 이력을 조회할 수 없는 학회입니다."]

    try:
        # get_client도 try 안에 있어야 한다. 밖에 두면 **자격증명이 없을 때
        # RuntimeError가 그대로 올라가 500이 된다** — 배포에서 실제로 그랬다.
        # 이 함수는 "외부 API 실패는 예외로 올리지 않는다"고 약속하고 있고,
        # 자격증명이 없는 것도 조회할 수 없는 사정 중 하나다. 심사 이력은
        # 부가 정보이므로 없으면 없는 대로 보여주고 페이지는 살린다.
        client = get_client(base)
        # 포럼은 forum_id 기준이지만 미수집 논문이 있어 openreview_id로 폴백한다
        # (detail.get_paper_detail의 forum 폴백과 같은 사정).
        replies = client.get_forum_replies(forum_id or openreview_id)
        # v1은 빈 리스트를 돌려준다 — 클라이언트가 알아서 막는다.
        edits = client.get_note_edits(openreview_id)
    except Exception as exc:
        log.warning("심사 이력 조회 실패 (%s): %s", openreview_id, exc)
        return [], False, [
            f"OpenReview 조회에 실패했습니다 ({type(exc).__name__}). "
            "잠시 후 다시 시도해 주세요."]

    revisions = build_revisions(edits)
    events = build_timeline(replies, revisions, db_reviews,
                            submission_id=openreview_id)

    caveats = [_REVIEW_NOTE]
    if base != V2:
        caveats.append(_V1_NOTE)
    elif any(e.kind == "author_revision" and not e.is_baseline for e in events):
        caveats.append(_BODY_NOTE)
    else:
        # baseline 하나뿐이거나 아예 없는 경우. 둘 다 '수정을 볼 수 없다'는 뜻이다.
        caveats.append(_NO_REVISION_NOTE)
    if not events:
        caveats.append("이 논문에는 공개된 리뷰·응답 기록이 없습니다.")
    return events, True, caveats


def get_paper_story(paper_id: int, use_llm: bool | None = None,
                    refresh: bool = False) -> PaperStory | None:
    """paper_id의 심사 서사를 조회한다. 논문이 없으면 None.

    use_llm=None이면 설정값(PAPER_ASSISTANT_USE_LLM)을 따른다. refresh=True면
    캐시를 무시하고 다시 만든다.
    """
    want_llm = config.USE_LLM if use_llm is None else use_llm

    with cursor() as cur:
        cur.execute(_PAPER_SQL, (paper_id,))
        row = cur.fetchone()
        if row is None:
            return None
        _, openreview_id, forum_id, title, venue, year, decision = row

        if not refresh:
            cached = _load_cache(cur, paper_id, want_llm)
            if cached is not None:
                return cached

        db_reviews = _db_reviews(cur, paper_id)
        cur.execute(_POINTS_SQL, (paper_id,))
        review_points = cur.fetchall()

    journey = get_submission_journey(paper_id)
    events, supported, caveats = _fetch_timeline(
        openreview_id, forum_id, venue, db_reviews)

    narrative = build_narrative(title, venue, decision, review_points,
                                events, journey, get_llm(want_llm))

    story = PaperStory(
        paper_id=paper_id, openreview_id=openreview_id, title=title,
        venue=venue, year=year, decision=decision,
        journey=journey, timeline=events, timeline_supported=supported,
        narrative=narrative, caveats=caveats)

    # 외부 조회가 실패한 결과는 캐시하지 않는다 — 일시적 장애를 30일 동안
    # 붙잡아두면 다음 요청들이 전부 빈 타임라인을 받는다 (db/stats._Cached와 같은 판단).
    if supported:
        _store_cache(paper_id, story, narrative.used_llm)
    return story


def _load_cache(cur, paper_id: int, want_llm: bool) -> PaperStory | None:
    try:
        cur.execute(_CACHE_GET_SQL, (paper_id,))
        row = cur.fetchone()
    except Exception as exc:     # 테이블 미생성(alembic 미적용) 등
        log.warning("story 캐시 조회 실패 — 캐시 없이 진행합니다: %s", exc)
        return None
    if row is None:
        return None

    payload, used_llm, built_at = row
    # 스텁으로 만든 캐시를 LLM 켠 요청에 내주지 않는다. 반대는 괜찮다 —
    # LLM으로 만든 요약은 스텁을 원한 요청에도 더 나은 답이다.
    if want_llm and not used_llm:
        return None
    if datetime.now(timezone.utc) - built_at > STALE_AFTER:
        return None

    try:
        story = PaperStory.model_validate(payload)
    except Exception as exc:
        # PaperStory가 바뀌면 옛 payload는 못 읽는다. 버리고 다시 만든다.
        log.info("story 캐시 형식이 맞지 않아 다시 만듭니다 (paper_id=%s): %s",
                 paper_id, exc)
        return None
    story.cached_at = built_at.isoformat()
    return story


def _store_cache(paper_id: int, story: PaperStory, used_llm: bool) -> None:
    try:
        with cursor(commit=True) as cur:
            cur.execute(_CACHE_PUT_SQL,
                        (paper_id, Jsonb(story.model_dump(mode="json")),
                         used_llm))
    except Exception as exc:
        # 캐시는 성능 장치일 뿐이라 저장 실패로 응답을 버리지 않는다.
        log.warning("story 캐시 저장 실패 (paper_id=%s): %s", paper_id, exc)
