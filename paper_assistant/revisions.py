"""논문 수정 이력 조회 — 저자가 리뷰를 받고 무엇을 고쳤는지.

세 번째 공개 API다. DB에는 최신 버전만 남으므로(load.upsert_paper가
openreview_id로 upsert) **OpenReview API를 실시간 조회**한다. get_paper_detail과
달리 네트워크를 타므로 느리고 실패할 수 있다 — 프론트는 사용자가 명시적으로
'수정 이력'을 눌렀을 때만 호출해야 한다.

핵심 어려움 두 가지:
1. edit은 부분 패치다. 그 시점에 바뀐 필드만 오므로 버전을 복원하려면 앞에서부터
   누적 적용해야 한다 (openreview_client.get_note_edits 주석 참고).
2. 최초 제출 edit이 항상 보이지는 않는다. 우리가 읽을 수 있는 가장 이른 edit이
   이미 3차 수정본일 수 있어, 그 이전 내용은 알 방법이 없다. 첫 edit을
   is_baseline=True로 표시해 '이 시점 이전은 미상'임을 명시한다.
"""
import difflib
import logging
from datetime import datetime, timedelta, timezone

from paper_assistant.db.connection import cursor
from paper_assistant.ingest.openreview_client import V2, VENUE_REGISTRY, get_client
from paper_assistant.schemas import (
    DiffSegment, FieldChange, PaperRevisions, RevisionEntry)

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# 추적할 필드와 표시 방식. 이 순서대로 화면에 나온다.
# authorids는 authors와 중복이라 뺐고, _bibtex·paperhash·venue 같은 시스템 생성
# 필드도 뺐다 — 저자가 고친 게 아니라 학회 시스템이 채우는 값이라 노이즈다.
TRACKED = [
    ("title", "제목", "text"),
    ("abstract", "초록", "text"),
    ("TLDR", "한 줄 요약", "text"),
    ("keywords", "키워드", "value"),
    ("primary_area", "분야", "value"),
    ("authors", "저자", "value"),
    ("pdf", "본문 PDF", "file"),
    ("supplementary_material", "보충 자료", "file"),
]

# invitation 접미사 → (kind, 한국어 라벨).
# rebuttal이 리뷰 대응 수정이라 가장 중요하다 — camera_ready는 이미 붙은 뒤라
# 리뷰와 무관한 편집이 섞인다.
# ⚠️ 부분 문자열 매칭이므로 **긴 것부터** 와야 한다. 'Revision'을 앞에 두면
# 'Camera_Ready_Revision'이 거기서 걸려 게재 확정본이 전부 '수정본'이 된다.
KINDS = [
    ("Camera_Ready_Revision", "camera_ready", "게재 확정본"),
    ("Rebuttal_Revision", "rebuttal", "리뷰 반영 수정"),
    ("Desk_Rejection", "withdrawal", "데스크 리젝"),
    ("Withdrawal", "withdrawal", "철회"),
    ("Revision", "revision", "수정본"),
    ("Submission", "submission", "최초 제출"),
]

# 초록 diff 조각 수 상한. 전면 재작성이면 조각이 수백 개 나와 화면이 죽는다.
MAX_SEGMENTS = 400

# 과거 버전 파일 링크. **edit id로만 받아진다** — edit content에 실린
# /pdf/<해시>.pdf 경로는 파일이 교체되는 순간 404가 된다(11개 버전 실측:
# 최신본 1개만 살아 있고 과거 10개는 전부 NotFoundError). 반면 이 경로는
# 표본 31건 전부 200 + application/pdf로 내려왔다.
EDIT_FILE_URL = "https://api2.openreview.net/notes/edits/attachment?id={edit_id}&name={field}"


def _file_url(field: str, edit_id: str | None) -> str | None:
    return EDIT_FILE_URL.format(edit_id=edit_id, field=field) if edit_id else None


def _unwrap(raw):
    """v2 content 값에서 실제 값을 꺼낸다.

    {"value": X} → X. 필드 삭제는 {"value": {"delete": true}}로 오므로(실측)
    value를 벗긴 뒤 한 번 더 확인해야 한다 — 안 하면 "{'delete': True}"라는
    문자열이 초록 diff에 그대로 찍힌다.
    """
    for _ in range(2):
        if not isinstance(raw, dict):
            break
        if raw.get("delete") is True:
            return None
        if "value" not in raw:
            break
        raw = raw["value"]
    if isinstance(raw, list):
        return ", ".join(str(x) for x in raw)
    if raw is None or isinstance(raw, str):
        return raw
    return str(raw)


def _classify(invitation: str) -> tuple[str, str]:
    suffix = invitation.split("/-/")[-1]
    for needle, kind, label in KINDS:
        if needle in suffix:
            return kind, label
    return "other", suffix.replace("_", " ")


def _word_diff(before: str, after: str) -> tuple[float, list[DiffSegment]]:
    """단어 단위 diff. 유사도와 조각 목록을 돌려준다."""
    a, b = before.split(), after.split()
    sm = difflib.SequenceMatcher(None, a, b)
    segs: list[DiffSegment] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            segs.append(DiffSegment(op="equal", text=" ".join(a[i1:i2])))
        else:  # replace는 delete+insert로 펼쳐 프론트가 두 색만 알면 되게 한다
            if i1 != i2:
                segs.append(DiffSegment(op="delete", text=" ".join(a[i1:i2])))
            if j1 != j2:
                segs.append(DiffSegment(op="insert", text=" ".join(b[j1:j2])))
    return sm.ratio(), segs[:MAX_SEGMENTS]


def _file_verb(before: str | None, after: str | None) -> str:
    """파일은 경로 해시가 바뀐 것만 보이므로 무슨 일이 있었는지만 말한다."""
    if before and after:
        return "교체됨"
    return "추가됨" if after else "삭제됨"


def _diff_fields(prev: dict, cur: dict,
                 prev_src: dict, cur_src: dict) -> list[FieldChange]:
    """누적된 두 버전 사이에서 바뀐 필드만 뽑는다.

    *_src는 파일 필드별로 '그 파일을 실어 나른 edit id'다. 파일 본체는 edit id로만
    받을 수 있어서(_file_url 주석), 전/후 링크를 만들려면 값과 별개로 출처를
    따라다녀야 한다.
    """
    changes: list[FieldChange] = []
    for field, label, kind in TRACKED:
        before, after = prev.get(field), cur.get(field)
        if before == after:
            continue
        # 이 필드가 처음 등장한 경우. 앞 버전에 없었다는 게 '삭제돼 있었다'는 뜻은
        # 아니다 — 단지 그때까지의 edit에 안 실렸을 뿐이라 diff로 단정하지 않는다.
        if before is None and field not in prev:
            continue
        if kind == "file":
            changes.append(FieldChange(
                field=field, label=label, kind="file",
                after=_file_verb(before, after),
                before_url=_file_url(field, prev_src.get(field)) if before else None,
                after_url=_file_url(field, cur_src.get(field)) if after else None))
        elif kind == "text" and before and after:
            ratio, segs = _word_diff(before, after)
            changes.append(FieldChange(
                field=field, label=label, kind="text", before=before, after=after,
                similarity=round(ratio, 3), segments=segs))
        else:
            changes.append(FieldChange(
                field=field, label=label, kind="value", before=before, after=after))
    return changes


def get_paper_revisions(paper_id: int) -> PaperRevisions | None:
    """paper_id로 OpenReview 수정 이력을 조회한다. 논문이 없으면 None."""
    with cursor() as cur:
        cur.execute(
            "SELECT openreview_id, venue FROM papers WHERE id = %s", (paper_id,))
        row = cur.fetchone()
    if row is None:
        return None
    openreview_id, venue = row

    base = (VENUE_REGISTRY.get(venue) or (None, None))[0]
    out = PaperRevisions(paper_id=paper_id, openreview_id=openreview_id,
                         supported=False)
    if base != V2:
        out.message = (
            f"{venue}는 수정 이력이 공개되지 않습니다 — 2023년 이전 학회는 "
            "구 API를 쓰는데, 저자가 고친 제목·초록·PDF가 외부에 열리지 않습니다. "
            "수정이 없었다는 뜻이 아니라 볼 수 없다는 뜻입니다.")
        return out

    try:
        edits = get_client(base).get_note_edits(openreview_id)
    except Exception as e:
        log.warning("edits 조회 실패 (%s): %s", openreview_id, e)
        out.message = f"OpenReview 조회에 실패했습니다 ({type(e).__name__})."
        return out

    out.supported = True
    if not edits:
        out.message = ("공개된 수정 이력이 없습니다. 저자가 고치지 않았거나, "
                       "학회가 수정 내역을 비공개로 둔 경우입니다.")
        return out

    state: dict[str, str | None] = {}
    src: dict[str, str] = {}   # 파일 필드 → 그 파일을 실어 나른 edit id
    for i, edit in enumerate(edits):
        patch = edit.get("note", {}).get("content") or {}
        nxt, nxt_src = dict(state), dict(src)
        for field, _, kind_ in TRACKED:
            if field not in patch:
                continue
            nxt[field] = _unwrap(patch[field])
            # 값이 있을 때만 출처를 갱신한다 — 삭제 edit에는 받을 파일이 없다
            if kind_ == "file" and nxt[field]:
                nxt_src[field] = edit.get("id", "")

        kind, label = _classify(edit.get("invitation", ""))
        ts = edit.get("tcdate") or edit.get("cdate") or 0
        out.revisions.append(RevisionEntry(
            revision_id=edit.get("id", ""),
            kind=kind, kind_label=label, timestamp=ts,
            date=datetime.fromtimestamp(ts / 1000, KST).strftime("%Y-%m-%d %H:%M"),
            changes=[] if i == 0 else _diff_fields(state, nxt, src, nxt_src),
            is_baseline=(i == 0),
        ))
        state, src = nxt, nxt_src

    return out
