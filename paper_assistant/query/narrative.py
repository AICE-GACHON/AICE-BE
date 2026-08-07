"""'무엇을 지적받아 무엇을 고쳤는가' 요약.

타임라인은 사건을 나열할 뿐이라 20~30개 항목을 사용자가 직접 읽어야 한다. 여기서
그 흐름을 네 덩어리(한 줄 요약 / 리뷰어 요구 / 저자 대응 / 결과)로 압축한다.

**근거의 한계가 이 모듈의 설계를 결정한다.** 우리가 관측할 수 있는 저자의 수정은
제목·초록·첨부파일 교체뿐이고, 실제 논문 본문이 어떻게 바뀌었는지는 PDF 안에 있어
알 수 없다. 그래서 인과("이 지적 때문에 이렇게 고쳤다")를 만들지 않는다 — 지적이
있었고, 저자가 이렇게 답했고, 초록에는 이런 변화가 있었다는 **순서**까지만 말한다.
프롬프트와 스텁 양쪽에 같은 제약을 건다.

llm.py 규약을 그대로 따른다: use_llm이 꺼져 있으면 llm이 None이고, 이때는
결정론적 스텁이 같은 스키마를 채운다 (비용 0). used_llm은 설정값이 아니라 실행
결과다.
"""
import json
import logging

from paper_assistant.llm import SONNET, SONNET_MAX_TOKENS
from paper_assistant.schemas import (
    StoryNarrative, SubmissionJourney, TimelineEvent)

log = logging.getLogger(__name__)

# 프롬프트에 실을 원문 상한. 코멘트가 18건까지 달리는 논문이 있어(실측) 전문을
# 다 넣으면 입력이 수만 자가 된다. 앞부분이 대개 요지라 잘라도 손실이 작다.
MAX_ITEMS = 6
MAX_CHARS = 1200

ASPECT_LABEL = {
    "novelty": "새로움·기여도",
    "experimental_scale": "실험 규모",
    "baselines": "베이스라인 비교",
    "clarity": "서술 명확성",
    "theoretical_soundness": "이론적 엄밀성",
    "reproducibility": "재현성",
    "related_work": "관련 연구",
    "significance": "중요도",
    "other": "기타",
}


def _asked_from_points(review_points: list[tuple]) -> list[tuple[str, int]]:
    """review_points(aspect, sentiment, text, from_unsplit)를 지적 빈도로 요약한다.

    from_unsplit_review 항목은 세지 않는다 — 강/약점 미분리 리뷰에서 나온 문장이라
    지적인지 확정할 수 없다(schemas.ReviewPointDetail 주석).

    'other'는 건수가 아무리 많아도 뒤로 보낸다. 분류에 실패한 문장이 모이는
    버킷이라 빈도로 줄 세우면 거의 항상 1등이 되는데(실측: 한 논문에서 기타 12건
    vs 중요도 1건), "이 논문은 기타를 지적받았습니다"는 아무 정보가 없다.
    파이프라인이 패턴 집계에서 other를 빼는 것과 같은 이유다.
    """
    counts: dict[str, int] = {}
    for aspect, sentiment, _, from_unsplit in review_points:
        if sentiment != "weakness" or from_unsplit:
            continue
        counts[aspect] = counts.get(aspect, 0) + 1
    ranked = sorted(counts.items(),
                    key=lambda kv: (kv[0] == "other", -kv[1], kv[0]))
    return [(ASPECT_LABEL.get(a, a), n) for a, n in ranked[:5]]


def _changed_from_timeline(timeline: list[TimelineEvent]) -> list[str]:
    """저자 쪽 사건(응답 수·수정본별 바뀐 필드)을 나열한다."""
    out: list[str] = []
    if rebuttals := _count_rebuttals(timeline):
        out.append(f"저자 응답 {rebuttals}건")

    for e in timeline:
        if e.kind != "author_revision" or e.is_baseline:
            continue
        if not e.changes:
            continue
        parts = []
        for c in e.changes:
            if c.kind == "text" and c.similarity is not None:
                parts.append(f"{c.label} {round((1 - c.similarity) * 100)}% 변경")
            elif c.kind == "file":
                parts.append(f"{c.label} {c.after}")
            else:
                parts.append(f"{c.label} 변경")
        out.append(f"{e.date[:10]} {', '.join(parts)}")
    return out[:6]


def _josa(word: str, with_final: str, without_final: str) -> str:
    """앞 글자의 받침에 따라 조사를 고른다 ('8건이' vs '1회가').

    스텁이 만든 문장은 사용자에게 그대로 나가는데, 개수에 따라 끝 글자가 바뀌어서
    조사를 고정하면 반드시 어느 한쪽이 틀린다.
    """
    if not word:
        return without_final
    last = word[-1]
    if not ("가" <= last <= "힣"):      # 숫자·영문으로 끝나면 판정 불가
        return without_final
    return with_final if (ord(last) - 0xAC00) % 28 else without_final


def _count_rebuttals(timeline: list[TimelineEvent]) -> int:
    return sum(1 for e in timeline if e.kind == "rebuttal")


def _count_revisions(timeline: list[TimelineEvent]) -> int:
    """실제로 내용이 바뀐 저자 수정본 수. baseline은 diff가 없어 세지 않는다."""
    return sum(1 for e in timeline
               if e.kind == "author_revision" and not e.is_baseline and e.changes)


def _outcome_note(decision: str, journey: SubmissionJourney) -> str:
    if journey.message:
        return journey.message
    return f"이 투고의 결과는 {decision}입니다."


def _stub(decision: str, review_points, timeline, journey,
          scope: str) -> StoryNarrative:
    ranked = _asked_from_points(review_points)
    changed = _changed_from_timeline(timeline)
    rebuttals, revisions = _count_rebuttals(timeline), _count_revisions(timeline)

    # 지적 쪽과 대응 쪽을 각각 있는 그대로 세어 붙인다. 스텁은 문장을 지어내지
    # 않고 개수만 말한다 — 해석은 LLM을 켰을 때의 몫이다.
    topic = f"{ranked[0][0]} 등" if ranked else None
    if rebuttals or revisions:
        did = " · ".join(filter(None, [
            f"저자 응답 {rebuttals}건" if rebuttals else None,
            f"저자 수정 {revisions}회" if revisions else None]))
        subject = did + _josa(did, "이", "가")
        headline = f"{topic}을 지적받았고, {subject} 있었습니다." if topic \
            else f"{subject} 기록돼 있습니다."
    elif topic:
        headline = f"{topic}을 지적받았고, 공개된 저자 대응 기록은 없습니다."
    else:
        headline = "공개된 리뷰 지적 항목이 없습니다."

    return StoryNarrative(
        headline=headline,
        reviewers_asked=[f"{label} {n}건" for label, n in ranked],
        authors_changed=changed,
        outcome_note=_outcome_note(decision, journey),
        evidence_scope=scope, used_llm=False)


def _facts(title: str, venue: str, decision: str, review_points,
           timeline: list[TimelineEvent], journey: SubmissionJourney) -> dict:
    """LLM에 넘길 구조화 입력. 원문은 상한을 걸어 자른다."""
    weaknesses = [t for a, s, t, unsplit in review_points
                  if s == "weakness" and not unsplit][:MAX_ITEMS]
    rebuttals = [e.text[:MAX_CHARS] for e in timeline
                 if e.kind == "rebuttal" and e.text][:MAX_ITEMS]

    revisions = []
    for e in timeline:
        if e.kind != "author_revision" or e.is_baseline or not e.changes:
            continue
        revisions.append({
            "date": e.date,
            "fields": [{
                "label": c.label,
                "changed_ratio": round(1 - c.similarity, 3)
                if c.similarity is not None else None,
                # 실제로 들어간/빠진 문구만 넘긴다. equal 조각까지 주면 초록
                # 전문을 두 번 싣는 셈이라 입력만 커지고 정보는 늘지 않는다.
                "inserted": " / ".join(
                    s.text for s in c.segments if s.op == "insert")[:MAX_CHARS],
                "removed": " / ".join(
                    s.text for s in c.segments if s.op == "delete")[:MAX_CHARS],
                "file_action": c.after if c.kind == "file" else None,
            } for c in e.changes],
        })

    return {
        "paper": {"title": title, "venue": venue, "decision": decision},
        "reviewer_weaknesses": weaknesses,
        "author_replies": rebuttals,
        "observed_revisions": revisions[:MAX_ITEMS],
        "review_updated_after_reply": any(
            e.kind == "review_update" for e in timeline),
        "resubmission": {
            "outcome": journey.outcome,
            "stops": [f"{s.venue} {s.decision}" for s in journey.stops],
        } if len(journey.stops) > 1 else None,
    }


_SYSTEM = (
    "You summarise the peer-review history of ONE paper for a Korean-speaking "
    "researcher who is about to submit similar work. Answer ONLY with a JSON "
    "object: {\"headline\": str, \"reviewers_asked\": [str], "
    "\"authors_changed\": [str], \"outcome_note\": str}. All values in Korean.\n"
    "headline: one sentence — what this paper was criticised for and what the "
    "authors did about it.\n"
    "reviewers_asked: 2-5 short items, the concrete objections reviewers raised. "
    "Name the specific thing ('QM9 한 데이터셋으로만 평가') over the generic "
    "category ('실험 부족').\n"
    "authors_changed: 2-5 short items, what the authors actually did — what they "
    "replied, and what changed in the title/abstract/attachments.\n"
    "outcome_note: one or two sentences on how it ended, including the "
    "resubmission trail if `resubmission` is present.\n"
    "EVIDENCE LIMITS — these are hard constraints, not style preferences:\n"
    "1. `observed_revisions` covers ONLY the title, abstract and attached files. "
    "The paper's body is a PDF we cannot read. Never write that the authors added "
    "an experiment, a baseline, a proof or a section to the paper — you cannot "
    "know that. You may write that they SAID they would (from author_replies), or "
    "that the abstract now mentions it (from `inserted`).\n"
    "2. Never assert causation between a specific criticism and a specific "
    "revision. The data has order, not cause. Write '지적이 있었고, 이후 초록에 "
    "…가 들어갔습니다', never '그 지적 때문에 …를 고쳤습니다'.\n"
    "3. `review_updated_after_reply` means a reviewer edited their review after "
    "the authors replied. The score BEFORE that edit is not public — never write "
    "that a score rose, fell, or by how much. You may say the review was revised.\n"
    "4. If author_replies and observed_revisions are both empty, say plainly that "
    "no author response is on record. Do not fill the gap with plausible guesses.\n"
    "5. Use only what is in the input. No outside knowledge about this paper.\n"
    "Write plain Korean prose. The reader has never seen this JSON, so do not use "
    "its key names, and do not mention 'rebuttal', 'diff' or 'revision' as jargon."
)


def build_narrative(title: str, venue: str, decision: str, review_points: list,
                    timeline: list[TimelineEvent], journey: SubmissionJourney,
                    llm) -> StoryNarrative:
    """서사 요약을 만든다. llm이 None이면 결정론적 스텁.

    review_points: (aspect, sentiment, text, from_unsplit_review) 튜플 목록.
    """
    scope = ("abstract_only"
             if any(e.kind == "author_revision" for e in timeline)
             else "replies_only")

    if llm is None:
        return _stub(decision, review_points, timeline, journey, scope)

    facts = _facts(title, venue, decision, review_points, timeline, journey)
    try:
        data = llm.json(SONNET, _SYSTEM,
                        json.dumps(facts, ensure_ascii=False),
                        max_tokens=SONNET_MAX_TOKENS)
    except Exception as exc:
        # 서사는 부가 정보다. LLM이 죽었다고 타임라인까지 못 보여줄 이유가 없어
        # 스텁으로 내려앉는다.
        log.warning("서사 요약 LLM 호출 실패 — 스텁으로 대체합니다: %s", exc)
        return _stub(decision, review_points, timeline, journey, scope)

    if not data or not data.get("headline"):
        log.warning("서사 요약 응답이 비어 스텁으로 대체합니다.")
        return _stub(decision, review_points, timeline, journey, scope)

    def _lines(key: str) -> list[str]:
        val = data.get(key) or []
        if isinstance(val, str):
            val = [val]
        return [str(x) for x in val][:5]

    return StoryNarrative(
        headline=str(data["headline"]),
        reviewers_asked=_lines("reviewers_asked"),
        authors_changed=_lines("authors_changed"),
        outcome_note=str(data.get("outcome_note")
                         or _outcome_note(decision, journey)),
        evidence_scope=scope, used_llm=True)
