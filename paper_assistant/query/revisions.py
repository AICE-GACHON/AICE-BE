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
import base64
import difflib
import logging
import os
import re
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta, timezone
from typing import Callable

from psycopg.types.json import Jsonb

from paper_assistant.db.connection import cursor
from paper_assistant.ingest.openreview_client import V2, VENUE_REGISTRY, get_client
from paper_assistant.pdf.extract import (
    MAX_BODY_PAGES,
    _looks_garbled,
    _MEDIA_PLACEHOLDER,
    BodyExtract,
    extract_body,
    image_similarity,
)
from paper_assistant.schemas import DiffSegment, FieldChange, PaperRevisions, RevisionEntry

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

# 본문(PDF 전체) diff 조각 수 상한. segment 수는 문서 길이가 아니라 수정 지점
# 개수에 비례하므로, 초록 대비 문서가 30~60배 길다고 상한을 그만큼 올릴 필요는
# 없다 — 5배 여유(2000)로 정상적인 대규모 리라이트는 수용하면서, 서로 다른 문서가
# 잘못 매칭된 병리적 케이스는 여전히 자른다. 실측이 쌓이면 조정 대상.
MAX_BODY_SEGMENTS = 2000

# 본문 diff 캐시 유효 기간. story.py의 STALE_AFTER와 값은 같지만 두 캐시가
# 독립적으로 무효화될 수 있도록 이 모듈 전용으로 따로 정의한다.
STALE_AFTER = timedelta(days=30)

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


# _word_diff가 쓰는 str.split()은 "\n\n"(문단 구분)을 그냥 공백으로 보고
# 지워버린다 — 제목/초록처럼 원래 문단이 하나뿐인 필드는 문제 없지만, 본문은
# 여러 문단이 한 "replace" 구간으로 묶이는 일이 흔해서(리뷰 반영으로 문단
# 여러 개가 조금씩 고쳐지면 SequenceMatcher가 그 사이 문단 경계를 못 살리고
# 통째로 하나의 replace로 묶는다) 이러면 문단 여러 개가 스페이스 하나로 이어
# 붙은 한 덩어리가 된다(실측: "...perform better in detail. Although
# generalist models..." — 두 개의 서로 다른 문단이 원래 "\n\n"으로 나뉘어
# 있었는데 하나로 뭉쳐 버림). "\n\n"을 일반 단어처럼 토큰 하나로 취급해
# SequenceMatcher 정렬에 참여시키고, 복원할 때만 다시 실제 개행으로 바꾼다.
_PARA_BREAK_TOKEN = "\x00PARA\x00"


def _tokenize_keep_paragraphs(text: str) -> list[str]:
    return text.replace("\n\n", f" {_PARA_BREAK_TOKEN} ").split()


def _detokenize_keep_paragraphs(tokens: list[str]) -> str:
    text = " ".join(tokens)
    for pattern in (f" {_PARA_BREAK_TOKEN} ", f"{_PARA_BREAK_TOKEN} ", f" {_PARA_BREAK_TOKEN}"):
        text = text.replace(pattern, "\n\n")
    return text.replace(_PARA_BREAK_TOKEN, "\n\n")


def _word_diff_multi_paragraph(before: str, after: str) -> tuple[float, list[DiffSegment]]:
    """_word_diff와 같지만 여러 문단이 섞인 텍스트에서 "\\n\\n" 경계를 보존한다.

    자리표시자(그림/표)가 안 낀 순수 텍스트 "replace" 구간 전용이다
    (_diff_replace_with_media는 위치 추적을 단어 개수로 하기 때문에 별도로
    다룬다 — 여기서 바꾸면 그 계산이 어긋난다).
    """
    a, b = _tokenize_keep_paragraphs(before), _tokenize_keep_paragraphs(after)
    sm = difflib.SequenceMatcher(None, a, b)
    segs: list[DiffSegment] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            segs.append(DiffSegment(op="equal", text=_detokenize_keep_paragraphs(a[i1:i2])))
        else:
            if i1 != i2:
                segs.append(DiffSegment(op="delete", text=_detokenize_keep_paragraphs(a[i1:i2])))
            if j1 != j2:
                segs.append(DiffSegment(op="insert", text=_detokenize_keep_paragraphs(b[j1:j2])))
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


def build_revisions(edits: list[dict]) -> list[RevisionEntry]:
    """edits(시간 오름차순)를 누적 적용해 리비전 목록으로 만든다.

    edit은 부분 패치라 앞에서부터 누적해야 그 시점의 전체 버전이 나온다
    (모듈 docstring 참고). 네트워크·DB를 타지 않는 순수 함수라 story.py가
    같은 edits로 타임라인을 만들 때 그대로 재사용한다 — 한쪽만 고쳐서 두 화면의
    diff가 달라지는 일을 막으려고 여기 하나만 둔다.
    """
    out: list[RevisionEntry] = []
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
        out.append(RevisionEntry(
            revision_id=edit.get("id", ""),
            kind=kind, kind_label=label, timestamp=ts,
            date=datetime.fromtimestamp(ts / 1000, KST).strftime("%Y-%m-%d %H:%M"),
            changes=[] if i == 0 else _diff_fields(state, nxt, src, nxt_src),
            is_baseline=(i == 0),
        ))
        state, src = nxt, nxt_src
    return out


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

    out.revisions = build_revisions(edits)
    return out


# --- 본문 수정 횟수 — 결과 표(SelectedPaper.revision_count)가 쓰는 요약값 -------
#
# ⚠️ len(revisions)를 세면 안 된다. build_revisions는 edit을 전부 담으므로
# 제목·초록만 고친 edit도 리비전 한 건으로 잡히는데, 화면이 묻는 것은 "본문을 몇
# 번 고쳤나"다. pdf FieldChange가 달린 리비전만 세야 그 질문에 답이 된다.
# baseline(최초 제출)은 changes가 비어 있어 자연히 빠진다 — 제출은 수정이 아니다.


def count_body_revisions(revisions: list[RevisionEntry]) -> int:
    """리비전 목록에서 본문 PDF가 실제로 교체된 건수. 네트워크·DB를 안 탄다."""
    return sum(1 for r in revisions
               if any(c.field == "pdf" for c in r.changes))


def get_body_revision_count(paper_id: int) -> int | None:
    """본문 PDF 교체 횟수. get_paper_revisions와 같은 비용(HTTP 1회)이고 PDF는 안 받는다.

    **None과 0은 다른 사실이다.** None은 "볼 수 없다" — 2023년 이전 학회는 구
    API라 저자가 고친 PDF가 외부에 안 열리고, 조회 실패나 이력 비공개도 여기 든다.
    0은 "이력은 봤는데 본문은 안 고쳤다"로 확인된 사실이다. 화면도 이 둘을 다르게
    쓴다(— / 없음).
    """
    revisions = get_paper_revisions(paper_id)
    if revisions is None or not revisions.supported or not revisions.revisions:
        return None
    return count_body_revisions(revisions.revisions)


# --- 본문(PDF 전체) diff — get_paper_revisions와 분리된 opt-in 확장 ------------
#
# build_revisions는 순수 함수라 story.py의 타임라인 생성이 그대로 재사용한다.
# 여기서부터는 실제로 PDF를 내려받아 파싱하므로 훨씬 비싸고, get_paper_revisions
# 호출 *이후*에 명시적으로만 붙는다 — story.py 쪽에는 이 비용이 새지 않는다.

_BODY_CACHE_GET_SQL = """
    SELECT payload, built_at FROM paper_body_diffs WHERE paper_id = %s
"""

_BODY_CACHE_PUT_SQL = """
    INSERT INTO paper_body_diffs (paper_id, payload, built_at)
    VALUES (%s, %s, now())
    ON CONFLICT (paper_id) DO UPDATE SET
        payload  = EXCLUDED.payload,
        built_at = EXCLUDED.built_at
"""


_PARA_BREAK = re.compile(r"\n\s*\n")


def _paragraphs(text: str) -> list[str]:
    """extract_full_text가 "\\n\\n"으로 이어붙인 레이아웃 블록을 문단으로 되돌린다."""
    return [p.strip() for p in _PARA_BREAK.split(text) if p.strip()]


def _diff_replace_with_media(replace_before: list[str], replace_after: list[str]) -> list[DiffSegment]:
    """그림/표 자리표시자가 낀 "교체" 구간을, v3(수정 후) 원문과 순서·자리가
    완전히 같도록 다시 조립한다.

    그림/표가 문단 배열 "중간"에서 빠지거나 새로 끼면(실측: 문장 가운데로
    그림이 들어오면서 앞/뒤 두 문단으로 쪼개짐) 문단 개수 자체가 달라져 이
    구간 전체가 "교체"로 잡힌다. 자리표시자를 빼고 텍스트끼리만 word-diff
    하면 무엇이 바뀌었는지는 정확히 잡히지만(_word_diff), 결과를 자리표시자
    뒤에 몰아 붙이면 "그림이 문장 정중앙 어디에 있었는지"가 사라져 v3를 다시
    읽었을 때 순서가 실제 논문과 달라진다 — **v3를 볼 때는 v3 원문과 자리
    하나까지 완전히 같아야 한다는 요구사항**을 못 지킨다.

    그래서 word-diff 결과를 자리표시자가 있던 정확한 단어 위치에서 다시
    잘라 그 자리에 끼워 넣는다: `_word_diff`는 "equal"+"insert" 조각을
    순서대로 이어 붙이면 after_chunk(자리표시자를 뺀 텍스트)를 한 글자도
    안 틀리고 복원한다는 성질이 있으므로, 자리표시자가 원래 있던 "몇 번째
    단어 뒤"인지만 알면 그 지점에서 조각을 쪼개 자리표시자를 꽂을 수 있다.
    "equal"+"delete" 조각은 같은 방식으로 before_chunk를 복원하므로 수정
    전(v2) 쪽 자리도 똑같이 맞춘다(수정 전 원문 재구성에 쓰임).
    """
    def split_parts(paragraphs: list[str]) -> tuple[list[str], dict[int, list[str]]]:
        text_parts: list[str] = []
        checkpoints: dict[int, list[str]] = {}
        word_count = 0
        for p in paragraphs:
            if _MEDIA_PLACEHOLDER.match(p):
                checkpoints.setdefault(word_count, []).append(p)
            else:
                text_parts.append(p)
                word_count += len(p.split())
        return text_parts, checkpoints

    before_text_parts, before_checkpoints = split_parts(replace_before)
    after_text_parts, after_checkpoints = split_parts(replace_after)

    if not before_text_parts and not after_text_parts:
        # 텍스트 없이 자리표시자만 있는 교체(그림 여러 개의 순서만 바뀌는 등) —
        # 문단 단위 delete/insert로 남기면 뒤의 _dedupe_placeholder_segments가
        # 그대로 처리한다.
        out: list[DiffSegment] = []
        for paras in before_checkpoints.values():
            out.extend(DiffSegment(op="delete", text=p + "\n\n") for p in paras)
        for paras in after_checkpoints.values():
            out.extend(DiffSegment(op="insert", text=p + "\n\n") for p in paras)
        return out

    before_chunk = "\n\n".join(before_text_parts)
    after_chunk = "\n\n".join(after_text_parts)
    _, word_segs = _word_diff(before_chunk, after_chunk)
    if word_segs and all(s.op == "equal" for s in word_segs):
        # 텍스트는 단어 하나 안 바뀌었다 — 이 구간이 "교체"로 잡힌 이유는
        # 순전히 그림/표 위치가 달라졌기 때문이다. equal로 조용히 넘기면
        # 이동했다는 사실 자체가 안 보이므로 moved로 표시한다.
        word_segs = [DiffSegment(op="moved", text=s.text) for s in word_segs]

    def flush(checkpoints: dict[int, list[str]], pos: int, op: str, out: list[DiffSegment]) -> None:
        for para in checkpoints.pop(pos, []):
            out.append(DiffSegment(op=op, text=para + "\n\n"))

    out: list[DiffSegment] = []
    a_pos = b_pos = 0
    for seg in word_segs:
        words = seg.text.split()
        n = len(words)
        advances_a = seg.op in ("equal", "delete", "moved")
        advances_b = seg.op in ("equal", "insert", "moved")

        flush(before_checkpoints, a_pos, "delete", out)
        flush(after_checkpoints, b_pos, "insert", out)

        if n == 0:
            out.append(seg)
            continue

        split_points: set[int] = set()
        if advances_a:
            split_points.update(p - a_pos for p in before_checkpoints if a_pos < p < a_pos + n)
        if advances_b:
            split_points.update(p - b_pos for p in after_checkpoints if b_pos < p < b_pos + n)

        if not split_points:
            out.append(seg)
        else:
            prev = 0
            for sp in sorted(split_points):
                piece = words[prev:sp]
                if piece:
                    out.append(DiffSegment(op=seg.op, text=" ".join(piece)))
                if advances_a:
                    flush(before_checkpoints, a_pos + sp, "delete", out)
                if advances_b:
                    flush(after_checkpoints, b_pos + sp, "insert", out)
                prev = sp
            remaining = words[prev:]
            if remaining:
                out.append(DiffSegment(op=seg.op, text=" ".join(remaining)))

        if advances_a:
            a_pos += n
        if advances_b:
            b_pos += n

    flush(before_checkpoints, a_pos, "delete", out)
    flush(after_checkpoints, b_pos, "insert", out)
    for paras in before_checkpoints.values():  # 방어적: 못 찾은 체크포인트
        out.extend(DiffSegment(op="delete", text=p + "\n\n") for p in paras)
    for paras in after_checkpoints.values():
        out.extend(DiffSegment(op="insert", text=p + "\n\n") for p in paras)

    # 자리표시자 바로 앞 텍스트 조각은 문단 구분을 위해 줄바꿈으로 끝나야
    # 한다 — 그렇지 않으면 이미지가 문장에 바로 붙어 보인다.
    for i, s in enumerate(out):
        if i > 0 and _MEDIA_PLACEHOLDER.match(s.text.strip()) and not out[i - 1].text.endswith("\n\n"):
            prev = out[i - 1]
            out[i - 1] = DiffSegment(op=prev.op, text=prev.text.rstrip() + "\n\n")
    if out and not out[-1].text.endswith("\n\n"):
        last = out[-1]
        out[-1] = DiffSegment(op=last.op, text=last.text + "\n\n")
    return out


def _paragraph_diff(before: str, after: str,
                    renumbered: dict[str, str] | None = None) -> tuple[float, list[DiffSegment]]:
    """문단 단위로 먼저 맞추고, 실제로 바뀐 문단만 단어 단위로 다시 diff한다.

    _word_diff를 본문 전체에 그대로 쓰면 text.split()이 모든 공백(문단 구분
    포함)을 지워버려 소제목이 어디서 끝나고 다음 문단이 어디서 시작하는지 알 수
    없는 한 덩어리 텍스트가 된다. 안 바뀐 문단은 통째로 살리고(줄바꿈 포함),
    실제로 바뀐 문단만 단어 단위 하이라이트를 입혀서 원래 구조를 유지한다.

    제목/초록/TLDR의 _word_diff 사용은 건드리지 않는다 — 그쪽은 애초에 여러
    문단으로 나뉠 일이 없는 짧은 필드라 이 문제가 없다.

    similarity는 _word_diff와 같은 의미(전체 단어 단위 유사도)로 별도 계산한다.
    문단 매칭 비율을 그대로 쓰면 "문단이 1개뿐인 텍스트에서 단어 하나만 바뀌어도
    유사도 0.0" 같은 왜곡이 생긴다.

    renumbered: attach_body_diffs가 이미지 재매칭으로 확인한 "번호만 다른 같은
    그림/표" 맵({"Figure 5": "Figure 7", ...}) — 자리표시자 병합에 쓴다
    (_merge_media_placeholders).
    """
    ratio = difflib.SequenceMatcher(None, before.split(), after.split()).ratio()

    before_paras = _paragraphs(before)
    after_paras = _paragraphs(after)
    sm = difflib.SequenceMatcher(None, before_paras, after_paras)
    segs: list[DiffSegment] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for para in before_paras[i1:i2]:
                segs.append(DiffSegment(op="equal", text=para + "\n\n"))
        elif tag == "replace":
            replace_before = before_paras[i1:i2]
            replace_after = after_paras[j1:j2]
            has_media = any(_MEDIA_PLACEHOLDER.match(p) for p in replace_before + replace_after)
            # ⚠️ **양쪽에 문단이 1개씩만 남았다고 "이 문단이 저 문단으로
            # 바뀌었다"는 보장은 없다.** SequenceMatcher는 문단 리스트를
            # 정렬하다가 둘 다 다른 문단과 안 묶였다는 이유만으로 위치상
            # 1:1 replace로 묶을 뿐, 내용이 실제로 대응한다고 확인한 게
            # 아니다(실측: v1의 "pixels..." 문단과 v2의 "Fig.6..." 문단이
            # 각각 앞뒤로 다른 문단과 매칭되고 남아 이 자리에서 1:1로
            # 묶였는데, 실제 문자 유사도는 0.036 — 완전히 무관한 문단이었다.
            # 반면 v1의 진짜 상대였던 "Fig.7..." 문단(유사도 0.997)은 자리를
            # 뺏겨 아래에 통째로 delete로 남아 중복까지 발생했다). 그래서
            # 유사도가 _MOVE_MATCH_THRESHOLD를 넘을 때만 "확실히 같은
            # 문단"으로 보고 단어 diff하고, 아니면 아래 else와 똑같이
            # 문단째 delete+insert로 남긴다 — 그래야 진짜 상대는
            # _match_moved_paragraphs의 전역 유사도 검색이 안전하게 찾는다.
            is_confident_pair = (len(replace_before) == 1 and len(replace_after) == 1
                                 and difflib.SequenceMatcher(
                                     None, replace_before[0], replace_after[0]).ratio()
                                 >= _MOVE_MATCH_THRESHOLD)
            if has_media:
                segs.extend(_diff_replace_with_media(replace_before, replace_after))
            elif is_confident_pair:
                _, word_segs = _word_diff_multi_paragraph(replace_before[0], replace_after[0])
                if word_segs:
                    last = word_segs[-1]
                    word_segs[-1] = DiffSegment(op=last.op, text=last.text + "\n\n")
                segs.extend(word_segs)
            else:
                # ⚠️ **문단이 여러 개 얽히면 단어 단위 diff가 엉뚱한 문단끼리
                # 섞는다.** 문단 개수가 같아도(예: 3개 vs 3개) 위치별로 1:1
                # 대응한다는 보장이 없다 — 실측: 부록에서 Figure 4/5/6/7를
                # 연달아 설명하며 "For a better understanding of X, we Y" 같은
                # 비슷한 문장이 반복되는 구간에서, 문단 두 개가 서로 무관한데도
                # 겹치는 단어(예: "Fig. 6", "also", "seen") 때문에 SequenceMatcher가
                # 한쪽 문단 중간을 다른 쪽 문단 중간과 짝지어 등 문단 전체가
                # 뒤죽박죽 섞인 채 재구성됐다(문단 경계까지 엉뚱한 곳에 생김).
                # 문단째 delete+insert로만 남기고, 실제로 거의 같은 문단이
                # 자리만 바뀐 경우는 이 뒤에 오는 _match_moved_paragraphs가
                # 문단 단위 유사도로 안전하게(문단마다 따로) 다시 찾아 단어
                # diff를 입힌다.
                for para in replace_before:
                    segs.append(DiffSegment(op="delete", text=para + "\n\n"))
                for para in replace_after:
                    segs.append(DiffSegment(op="insert", text=para + "\n\n"))
        elif tag == "delete":
            for para in before_paras[i1:i2]:
                segs.append(DiffSegment(op="delete", text=para + "\n\n"))
        else:  # insert
            for para in after_paras[j1:j2]:
                segs.append(DiffSegment(op="insert", text=para + "\n\n"))
    # ⚠️ **한 번의 매칭이 새 매칭 기회를 만들어낸다.** 문단 하나가 다른
    # 이유로도 살짝 바뀌어서(예: 문단 끝에 몇 단어가 붙음) 통째로
    # delete+insert된 뒤, _match_moved_paragraphs가 그 문단 쌍을 찾아 word-diff로
    # 쪼개면 그 결과로 "새로운" 작은 insert 조각(방금 붙은 몇 단어)이 생긴다.
    # 만약 그 몇 단어가 원래 다른 문단(경계 너머)에서 옮겨온 것이었다면,
    # 그 문단 쪽엔 같은 내용의 delete가 독립적으로 남아 있는데, 처음
    # 한 번의 패스로는 방금 새로 생긴 insert와 짝지을 기회가 없다(실측:
    # "Although generalist models...investigate whether"로 끝나던 문단 뒤에
    # "the diffusion-based...predictions"가 있었는데, 수정 후엔 그 구절이
    # 앞 문단 끝으로 옮겨 붙었다 — 앞 문단은 그 자체로도 살짝 수정돼 있어
    # 문단째 delete+insert로 처리되고, 뒤 문단은 별도 1:1 교체로 그 구절만
    # delete로 남는다. 첫 패스가 앞 문단을 word-diff로 쪼개고 나서야 그
    # 구절이 insert로 따로 드러나는데, 이미 끝난 패스라 뒤 문단의 delete와
    # 짝지어지지 못해 같은 구절이 삭제+추가로 중복 표시됐다). 더 안 바뀔
    # 때까지 반복하면 안전하게(같은 유사도 기준으로) 이런 연쇄 기회를 마저
    # 잡는다 — 매 패스는 검증된 로직 그대로이므로 새 오탐 위험은 없다.
    for _ in range(5):
        before_state = [(s.op, s.text) for s in segs]
        segs = _match_moved_paragraphs(segs)
        if [(s.op, s.text) for s in segs] == before_state:
            break
    segs = _merge_media_placeholders(segs, renumbered or {})
    return round(ratio, 3), segs[:MAX_BODY_SEGMENTS]


# 문단 전체가 이동만 됐을 때 "완전히 다른 두 문단"이 아니라 "같은 문단이
# 옮겨졌다(+살짝 고쳐졌을 수도 있다)"로 볼 유사도 기준. 문단은 박스 프롬프트
# 보다 훨씬 길고 서로 구별되는 문장이라 임계값을 조금 더 높게 잡았다
# (_BOX_MATCH_THRESHOLD 참고 — 같은 설계, 대상만 다르다).
_MOVE_MATCH_THRESHOLD = 0.6


_MOVE_MIN_WORDS = 6


def _match_moved_paragraphs(segs: list[DiffSegment]) -> list[DiffSegment]:
    """그림·표가 문서 안에서 자리를 옮기면 그 앞뒤 문단들의 정렬이 깨져,
    내용이 안 바뀐 문단인데도 delete+insert로 잘못 쪼개지는 경우가 있다
    (실측: Figure 2가 다른 섹션으로 옮겨간 리비전에서, delete 31개 중 11개가
    insert 쪽에 사실상 동일한 문단을 갖고 있었다 — SequenceMatcher는 정렬이
    깨진 두 지점을 "관계 없는 삭제"와 "관계 없는 삽입"으로만 본다).

    ⚠️ **문단 전체 delete/insert만으로는 부족하다.** 그림/표가 문단 "중간"에서
    빠지거나 새로 끼면 옆 문단이 통째로 합쳐지거나 쪼개져서, 한쪽은 원래
    문단 그대로(delete)인데 반대쪽은 그 문단이 다른 문단과 합쳐진 word-diff
    조각(insert)으로만 나타난다(실측: "Instead, we opt for a gradient-free..."
    문단 뒤에 있던 표가 앞으로 옮겨가자, 뒷문단과 합쳐지면서 이 문단이
    delete 한 번 + word-diff의 insert 조각 한 번, 총 두 번 나타남). 그래서
    _paragraphs()가 나눈 원래 문단인지 확인하지 않고, **일정 단어 수
    이상인 delete/insert는 전부** 후보로 본다 — 표현이 짧은 접속사·숫자
    조각까지 잡히면 엉뚱한 것끼리 묶일 수 있어 최소 단어 수로 걸러낸다.

    완전히 같으면 "이동만 됐다"고 보고 op="moved"로 표시한다(내용이 안
    바뀌었는데 삭제·삽입 색을 칠하면 "내용이 바뀐 것처럼" 보여 오해를 준다).
    살짝 다르면 "이동+수정"으로 보고 word-diff한다. 그림/표 자리표시자는
    _dedupe_placeholder_segments가 따로 다루므로 여기서는 뺀다.

    ⚠️ **살아남는 자리는 insert(수정 후=v3) 쪽이어야 한다.** delete 쪽(v2의
    옛 위치)을 남기면 v3를 보는 화면인데 문단이 v2 시절 자리에 나타나는
    꼴이 되어, "v3 원문과 순서·자리가 완전히 같아야 한다"는 요구를 어긴다.
    """

    def is_substantial(text: str) -> bool:
        stripped = text.strip()
        return (bool(stripped) and len(stripped.split()) >= _MOVE_MIN_WORDS
                and not _MEDIA_PLACEHOLDER.match(stripped))

    delete_idxs = [i for i, s in enumerate(segs) if s.op == "delete" and is_substantial(s.text)]
    insert_idxs = [i for i, s in enumerate(segs) if s.op == "insert" and is_substantial(s.text)]
    if not delete_idxs or not insert_idxs:
        return segs

    candidates = []
    for di in delete_idxs:
        d_text = segs[di].text.strip()
        for ii in insert_idxs:
            i_text = segs[ii].text.strip()
            ratio = difflib.SequenceMatcher(None, d_text, i_text).ratio()
            if ratio >= _MOVE_MATCH_THRESHOLD:
                candidates.append((ratio, di, ii))
    candidates.sort(key=lambda x: -x[0])

    replacement: dict[int, list[DiffSegment]] = {}
    used_delete: set[int] = set()
    for ratio, di, ii in candidates:
        if di in used_delete or ii in replacement:
            continue
        d_text, i_text = segs[di].text.strip(), segs[ii].text.strip()
        if d_text == i_text:
            # 저자가 실제로 옮긴 건 맞지만 내용은 한 글자도 안 바꿨다 — 삭제(빨강)나
            # 추가(초록)로 칠하면 "내용이 바뀌었다"는 뜻이 되어 버려 오해를 준다.
            # 그래서 equal이 아니라 별도의 moved로 표시해 "위치만 옮겨졌다"를
            # 구분해서 보여준다.
            replacement[ii] = [DiffSegment(op="moved", text=segs[ii].text)]
        else:
            _, word_segs = _word_diff(d_text, i_text)
            if word_segs:
                last = word_segs[-1]
                word_segs[-1] = DiffSegment(op=last.op, text=last.text + "\n\n")
            replacement[ii] = word_segs
        used_delete.add(di)

    out: list[DiffSegment] = []
    for i, s in enumerate(segs):
        if i in used_delete:
            continue  # 매칭된 delete는 버리고 insert(v3 실제 위치) 자리에 합친 결과를 넣는다
        out.extend(replacement.get(i, [s]))
    return out


_LABEL_WORD = {"Figure": "그림", "Table": "표", "Algorithm": "알고리즘", "Box": "박스", "Equation": "수식"}


def _label_to_placeholder(label: str) -> str | None:
    prefix, _, number = label.partition(" ")
    word = _LABEL_WORD.get(prefix)
    return f"({word} {number})" if word and number else None


def _merge_media_placeholders(segs: list[DiffSegment], renumbered: dict[str, str]) -> list[DiffSegment]:
    """SequenceMatcher가 문단 리스트 전역에서 정렬을 맞추다 보면, 앞뒤 문단이 많이
    달라진 탓에 완전히 동일한 "(그림 N)"/"(표 N)" 자리표시자조차 같은 equal
    opcode로 묶이지 못하고 서로 다른 delete/insert opcode에 따로 떨어져 나오는
    경우가 있다 — 텍스트는 안 바뀌었는데 그림/표가 중복으로 보이는 원인.
    opcode 경계와 무관하게 최종 segment 리스트 전체에서 각 delete 자리표시자의
    "진짜 상대"(수정 후 번호)를 찾아 하나로 합친다.

    renumbered: 이미지 매칭 단계에서 이미 확인된 "번호만 다른 같은 그림/표"
    맵({"Figure 5": "Figure 7", ...}, attach_body_diffs 참고). delete 쪽
    번호가 이 맵에 있으면 그 번호로 바뀐 자리표시자를 찾고, 없으면(번호가
    안 바뀐 경우) 같은 번호 그대로를 찾는다.

    ⚠️ **문자열이 같다고 무조건 같은 상대로 보면 안 된다.** 번호가 돌려막기로
    재배치되면(실측: v2 Figure 5→7, 6→5, 7→6 — 3개가 한 바퀴 돌아감) "(그림
    6)"이라는 문자열이 delete 쪽(v2의 옛 Figure 6)과 insert 쪽(v3의 새
    Figure 6, 내용은 완전히 다른 그림) 양쪽에 동시에 등장한다. 문자열만 보고
    맞춰버리면 서로 무관한 두 그림을 하나로 합쳐버린다 — renumbered 맵으로
    "이 delete가 진짜 찾아야 할 문자열"을 미리 확정해야 이런 우연한 충돌을
    피한다.

    ⚠️ **살아남는 자리는 insert(수정 후=v3) 쪽이어야 한다.** delete 쪽(옛
    자리)을 살리면, v3 화면인데 그림이 v2 시절 옛 섹션 위치에 나타나는
    꼴이 되어 "지금 문서에서 실제로 어디 있는지"와 어긋나 오해를 준다
    (실측: Figure 2가 RELATED WORK에서 METHOD로 옮겨갔는데 delete 쪽을
    남기면 RELATED WORK 자리에 그대로 나타남). 그리고 이 병합 자체가
    "내용은 같은데 자리가 갈라져 있었다"는 뜻이므로 equal이 아니라
    moved로 표시해 위치가 바뀌었다는 사실을 그대로 드러낸다.

    ⚠️ **번호가 "재사용"되는 경우도 있다.** 다른 그림이 삭제되면서 뒷번호가
    당겨지면(실측: v1 Figure 4 삭제 → v1 Figure 5가 v2에서 "Figure 4"가
    됨), v2의 새 "(그림 4)"는 재배치 맵의 값(after-label)일 뿐 키가
    아니므로 위의 "키가 재배치 대상이면 풀어준다" 조건만으로는 못 잡는다.
    v1에 원래 있던(그리고 v2에서 진짜로 삭제된) 옛 "Figure 4" 문단과
    문자열이 우연히 같아서 SequenceMatcher가 이 둘을 equal로 잘못
    묶어버린다 — 재배치 맵의 값(target) 쪽도 같이 풀어줘야 한다.

    ⚠️ **체인이 이어질 때는 "갈 곳이 있는" delete를 먼저 처리해야 한다.**
    위 케이스를 풀면 delete가 두 개 생긴다 — v1의 옛 Figure 4(진짜 삭제,
    target=자기 자신) 그리고 v1의 옛 Figure 5(Figure 4로 재배치, target=
    "그림 4"). 둘 다 target이 "(그림 4)"인 insert 자리 하나를 놓고 경쟁하는데,
    "자기 자신이 target인"(=재배치 맵의 키가 아닌, 즉 갈 곳이 정해지지 않은)
    delete가 먼저 그 자리를 채가면, 정작 재배치돼야 할 delete는 상대를
    잃고 그대로 delete로 남아버린다(반대로 moved가 나와야 할 자리가 안
    나옴). 라벨이 재배치 맵의 키인(=명확한 목적지가 있는) delete를 항상
    먼저 처리해서, 목적지 없는 delete(=진짜 삭제)가 남의 자리를 먼저
    차지하지 못하게 한다.
    """
    placeholder_map: dict[str, str] = {}
    for bl, al in renumbered.items():
        b_ph, a_ph = _label_to_placeholder(bl), _label_to_placeholder(al)
        if b_ph and a_ph:
            placeholder_map[b_ph] = a_ph
    renamed_targets = set(placeholder_map.values())

    def label_of(text: str) -> str | None:
        stripped = text.strip()
        return stripped if _MEDIA_PLACEHOLDER.match(stripped) else None

    # 번호가 돌려가며 재배치되거나(3개가 한 바퀴 도는 등) 삭제로 뒷번호가
    # 당겨져 재사용되면, 문단 정렬 단계의 SequenceMatcher가 "(그림 4)"를
    # 우연히 "equal"로 잘못 정렬해 버릴 수 있다(문자열만 같을 뿐 서로 다른
    # 그림 — 실측: 3개 순환 재배치, 그리고 삭제로 인한 번호 재사용 둘 다
    # 발생). 이 라벨이 재배치 맵의 키(수정 전 번호)든 값(수정 후 번호)이든
    # 관여돼 있으면 무조건 delete+insert로 도로 풀어서 아래 매칭 로직이
    # 진짜 상대를 다시 찾게 한다.
    expanded: list[DiffSegment] = []
    for s in segs:
        lbl = label_of(s.text)
        if s.op == "equal" and lbl and (lbl in placeholder_map or lbl in renamed_targets):
            expanded.append(DiffSegment(op="delete", text=s.text))
            expanded.append(DiffSegment(op="insert", text=s.text))
        else:
            expanded.append(s)
    segs = expanded

    insert_positions: dict[str, list[int]] = {}
    for i, s in enumerate(segs):
        if s.op == "insert":
            label = label_of(s.text)
            if label:
                insert_positions.setdefault(label, []).append(i)

    to_drop: set[int] = set()
    out = list(segs)
    # 목적지가 정해진(라벨이 placeholder_map의 키인) delete를 먼저 처리한다.
    # 번호가 삭제로 재사용되면 "갈 곳 없는"(target=자기 자신) delete와
    # "재배치될"(target=다른 번호) delete가 같은 번호의 insert 자리 하나를
    # 놓고 경쟁하는데, 갈 곳 없는 쪽이 먼저 채가면 진짜 재배치 delete가
    # 상대를 잃는다(실측: v1 Figure4 삭제 + Figure5→4 재배치가 겹친 경우).
    delete_indices = sorted(
        (i for i, s in enumerate(segs) if s.op == "delete" and label_of(s.text)),
        key=lambda i: label_of(segs[i].text) not in placeholder_map,
    )
    for i in delete_indices:
        label = label_of(segs[i].text)
        target = placeholder_map.get(label, label)  # 재배치 안 됐으면 자기 자신을 찾는다
        positions = insert_positions.get(target)
        if not positions:
            continue
        j = positions.pop(0)
        out[j] = DiffSegment(op="moved", text=segs[j].text)
        to_drop.add(i)
    return [s for k, s in enumerate(out) if k not in to_drop]


@dataclass
class _VersionExtract:
    """PDF 한 버전에서 뽑은 것 전부 — 본문 diff·그림·표 이미지가 같은 다운로드를
    공유하도록 한 번에 묶는다(URL당 한 번만 내려받게 하는 캐시 단위).

    표는 그림과 똑같이 이미지로 다룬다 — find_tables()로 셀 구조까지 뽑아본 적이
    있는데 병합된 셀을 못 풀어내 원본과 많이 달라져서, 텍스트 재구성 대신 표
    영역을 그대로 크롭해 원본과 최대한 비슷하게 보여주는 쪽을 택했다.
    """
    text: str | None
    images: dict[str, bytes] = dataclass_field(default_factory=dict)  # "Figure N"/"Table N" → PNG
    box_texts: dict[str, str] = dataclass_field(default_factory=dict)  # "Box <해시>" → 내용 텍스트
    signatures: dict[str, bytes] = dataclass_field(default_factory=dict)  # 그림·표·알고리즘 → 시각 시그니처(폴백용)
    captions: dict[str, str] = dataclass_field(default_factory=dict)  # 그림·표·알고리즘 → 캡션 설명문(번호 제외)


def _default_fetch_version(url: str) -> "_VersionExtract | None":
    """URL → PDF 다운로드 → 본문 텍스트 + 그림·표 이미지를 한 번에 뽑는다.

    404(오래된 edit-id) 등은 get_bytes가 재시도 없이 바로 예외를 던지므로 여기서
    잡는다 — 하나의 죽은 링크가 나머지 transition들의 diff까지 막으면 안 된다.
    본문 텍스트가 없거나 깨졌으면(스캔본 등) 그림·표도 신뢰할 수 없다고 보고
    전체를 None 처리한다.

    필요한 것 전부를 extract_body 한 번으로 받는다 — 예전엔 추출기 넷을 따로
    불러 같은 pdf 바이트를 네 번 열고 페이지 분석을 네 번 반복했다.

    버전이 둘 이상이면 이 함수 대신 _prefetch_default가 돌아간다(다운로드와
    추출을 나눠 동시에 처리). 이 함수는 그 경로를 못 쓰는 경우 — 가져올
    URL이 하나뿐일 때 — 를 위한 단건 경로다.
    """
    pdf_bytes = _download_pdf(url)
    if pdf_bytes is None:
        return None
    return _version_from_body(extract_body(pdf_bytes, max_pages=MAX_BODY_PAGES))


def _download_pdf(url: str) -> bytes | None:
    """PDF 한 건 다운로드. 죽은 링크(404 등)는 None으로 알린다.

    다운로드만 따로 떼어 둔 이유는 추출과 병렬화 방식이 달라서다 — 다운로드는
    네트워크 대기라 스레드로 충분히 겹치지만, 추출은 GIL에 묶여 스레드로는
    안 겹친다(_prefetch_default 참고).
    """
    try:
        return get_client(V2).get_bytes(url)
    except Exception as exc:
        log.warning("본문 diff용 PDF 다운로드 실패 (%s): %s", url, exc)
        return None


def _version_from_body(body: BodyExtract) -> "_VersionExtract | None":
    """extract_body 결과 → _VersionExtract. 못 믿을 추출이면 None."""
    if body.text is None or _looks_garbled(body.text):
        return None
    return _VersionExtract(text=body.text, images=body.images, box_texts=body.box_texts,
                           signatures=body.signatures, captions=body.captions)


# PDF를 동시에 내려받을 최대 스레드 수. 논문 한 편의 pdf 교체 지점은 보통
# 2~5개(=고유 URL 3~6개)라 이보다 늘려도 더 가져올 게 없고, OpenReview가
# 429로 막을 위험만 커진다 — get_bytes의 재시도는 지수 백오프(5s, 10s, 20s...)
# 라 한 번 걸리면 순차보다 오히려 느려진다.
_MAX_DOWNLOAD_WORKERS = 4

# 추출(파싱)을 동시에 돌릴 최대 **프로세스** 수. 스레드가 아니다 — 실측:
# 논문 4편을 스레드로 동시에 추출하면 순차 대비 0.95배로 오히려 느리고
# (53.7s → 56.6s), 프로세스로 돌리면 1.75배 빨라진다(→ 30.7s). 추출 시간의
# 대부분을 PyMuPDF의 C 코드가 아니라 우리 파이썬 로직(블록 병합·영역 판단)이
# 쓰기 때문에 GIL에 묶여 스레드로는 안 겹친다.
#
# 다운로드 쪽과 값이 같지만 근거는 완전히 별개다 — 이쪽 상한은 레이트리밋이
# 아니라 서버의 CPU·메모리다(자식 하나가 PDF 원본 + 크롭 이미지를 통째로
# 들고 있고, 이 엔드포인트는 캐시 미스마다 돈다).
_MAX_PARSE_WORKERS = 4


def _pdf_urls(paper_revisions: PaperRevisions) -> list[str]:
    """본문 diff에 필요한 PDF URL 전부 — 등장 순서대로, 중복 없이.

    attach_body_diffs의 루프가 URL을 만나는 순서와 같은 순서로 모은다(dict는
    삽입 순서를 보존한다). 순서가 결과에 영향을 주지는 않지만, 로그·예외가
    순차 경로와 같은 순서로 나오는 편이 디버깅에 낫다.
    """
    seen: dict[str, None] = {}
    for entry in paper_revisions.revisions:
        for change in entry.changes:
            if change.field != "pdf" or change.kind != "file":
                continue
            for url in (change.before_url, change.after_url):
                if url:
                    seen.setdefault(url, None)
    return list(seen)


def _parse_executor(workers: int):
    """추출용 실행기. 함수로 빼 둔 건 테스트가 프로세스를 안 띄우고도 이
    경로(다운로드/추출 분리, 결과 조립)를 검증할 수 있게 하기 위해서다.
    """
    return ProcessPoolExecutor(max_workers=workers)


def _prefetch_default(urls: list[str]) -> dict[str, "_VersionExtract | None"]:
    """실제 경로 — 다운로드는 스레드로, 추출은 프로세스로 동시에 처리한다.

    **두 단계를 다르게 병렬화하는 이유**(전부 실측):
      - 다운로드는 네트워크 대기(실측: OpenReview 중앙값 1.0s/건)라 스레드로
        온전히 겹친다.
      - 추출은 15~25s/건인데 그 시간을 파이썬 로직이 쓴다 — 스레드로는 GIL에
        묶여 안 겹치고(순차 53.7s vs 스레드 56.6s로 오히려 손해) 프로세스로만
        실제로 겹친다(30.7s).

    자식 프로세스가 import하는 건 paper_assistant.pdf.extract 하나뿐이다
    (0.10s). 이 모듈(revisions)을 넘기면 psycopg·openreview 클라이언트까지
    딸려 들어가 0.75s가 된다 — 그래서 자식에 보내는 건 "URL"이 아니라 이미
    받아 둔 **pdf 바이트**이고, 네트워크·인증은 전부 부모가 맡는다.

    예외는 그대로 올려보낸다 — 순차 경로에서도 모든 URL을 결국 한 번씩
    가져오므로 "어떤 PDF가 터지면 요청 전체가 실패한다"는 동작이 같다.
    죽은 링크(404) 같은 흔한 실패는 _download_pdf가 None으로 돌려준다.
    """
    with ThreadPoolExecutor(max_workers=min(len(urls), _MAX_DOWNLOAD_WORKERS)) as pool:
        downloads = {url: pool.submit(_download_pdf, url) for url in urls}
    # with 블록을 빠져나온 시점에 모든 작업이 끝나 있다 — 결과를 여기서 읽으면
    # 중간에 예외가 올라와도 남은 스레드가 매달려 있지 않다.
    pdfs = {url: future.result() for url, future in downloads.items()}

    out: dict[str, "_VersionExtract | None"] = {url: None for url in urls}
    live = [url for url, data in pdfs.items() if data is not None]
    if not live:
        return out

    workers = min(len(live), _MAX_PARSE_WORKERS, os.cpu_count() or 1)
    try:
        with _parse_executor(workers) as pool:
            extracts = {url: pool.submit(extract_body, pdfs[url], MAX_BODY_PAGES)
                        for url in live}
        bodies = {url: future.result() for url, future in extracts.items()}
    except (OSError, BrokenProcessPool) as exc:
        # 프로세스를 못 띄우거나(권한이 막힌 컨테이너 등) 자식이 죽은 경우.
        # 추출 자체가 던진 예외는 여기 안 걸리고 그대로 올라간다 — 이건
        # "병렬로 못 돌린다"는 신호일 때만 이 프로세스에서 다시 한다.
        log.warning("추출 병렬 실행 실패 — 이 프로세스에서 순차로 처리합니다: %s", exc)
        bodies = {url: extract_body(pdfs[url], max_pages=MAX_BODY_PAGES) for url in live}

    for url, body in bodies.items():
        out[url] = _version_from_body(body)
    return out


def _prefetch_versions(urls: list[str],
                       fetch: Callable[[str], "_VersionExtract | None"]
                       ) -> dict[str, "_VersionExtract | None"]:
    """URL들을 동시에 가져온다 — {url: _VersionExtract | None}.

    실제 경로는 _prefetch_default(다운로드=스레드, 추출=프로세스)로 간다.
    주입된 fetch(테스트용 가짜)는 프로세스에 보낼 수 없으므로(지역 함수라
    피클되지 않는다) 스레드로 동시에 부른다 — 오케스트레이션 계약(같은 URL은
    한 번만, 동시 호출, 예외 전파)은 양쪽이 같다.
    """
    if fetch is _default_fetch_version:
        return _prefetch_default(urls)
    with ThreadPoolExecutor(max_workers=min(len(urls), _MAX_DOWNLOAD_WORKERS)) as pool:
        futures = {url: pool.submit(fetch, url) for url in urls}
    return {url: future.result() for url, future in futures.items()}


def _data_uri(png_bytes: bytes | None) -> str | None:
    if not png_bytes:
        return None
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def _numbered(labels) -> list[str]:
    """'Figure 2'보다 'Figure 10'이 뒤에 오도록(문자열 정렬이면 '10' < '2') 정렬."""
    def key(label: str):
        m = re.search(r"\d+", label)
        return int(m.group()) if m else 0
    return sorted(labels, key=key)


# 박스는 저자 번호가 없어 내용 해시로 식별한다(_box_regions_and_texts 참고) — 완전히
# 같으면 해시도 같아 자동으로 매칭되지만, 살짝만 고쳐도 해시가 통째로
# 달라져 "관계 없는 두 박스"처럼 보인다. 이 임계값 이상 비슷하면 "같은
# 박스가 수정됨"으로, 미만이면 "박스 하나는 없어지고 다른 박스가 새로
# 생김"으로 본다. 정밀한 값이 아니라 두 극단(거의 같은 글 vs 완전히 다른
# 글)을 가르는 용도라 넉넉히 잡았다.
_BOX_MATCH_THRESHOLD = 0.5


def _match_boxes(before_only: list[str], after_only: list[str],
                 before_texts: dict[str, str], after_texts: dict[str, str],
                 before_images: dict[str, bytes], after_images: dict[str, bytes]
                 ) -> dict[str, str]:
    """해시가 안 맞은 박스 라벨끼리 유사도로 짝짓는다 — {before_label: after_label}.

    텍스트가 있으면(대부분의 코드·프롬프트 박스) SequenceMatcher 비율로
    비교한다 — 박스 내용이 본질적으로 텍스트라 픽셀 비교보다 정확하고,
    렌더링 위치·안티앨리어싱에 흔들리지도 않는다(_box_content_hash가 픽셀
    해시 대신 텍스트 해시를 쓰는 것과 같은 이유). 텍스트가 없는(순수 이미지)
    박스만 PNG 바이트 크기 근접도로 대략 비교한다 — 완전히 다른 그림인지
    비슷한 크기의 같은 그림인지 정도만 가리는 용도라 정밀할 필요는 없다.

    그리디로 가장 비슷한 쌍부터 확정한다 — 박스 개수가 페이지당 몇 개
    수준이라 최적 매칭(헝가리안 알고리즘 등)까지는 과하다고 판단했다.
    """
    candidates = []
    for bl in before_only:
        for al in after_only:
            bt, at = before_texts.get(bl, ""), after_texts.get(al, "")
            if bt or at:
                ratio = difflib.SequenceMatcher(None, bt, at).ratio()
            else:
                bsz, asz = len(before_images.get(bl, b"")), len(after_images.get(al, b""))
                ratio = 1 - abs(bsz - asz) / max(bsz, asz, 1)
            candidates.append((ratio, bl, al))
    candidates.sort(key=lambda x: -x[0])

    matched: dict[str, str] = {}
    used_after: set[str] = set()
    for ratio, bl, al in candidates:
        if ratio < _BOX_MATCH_THRESHOLD:
            break
        if bl in matched or al in used_after:
            continue
        matched[bl] = al
        used_after.add(al)
    return matched


# 캡션 설명문이 충분히 길면(우연히 같을 위험이 낮으면) 이미지 픽셀보다
# 훨씬 믿을 만하다 — 실측: 같은 그림인데 번호가 재배치되며 페이지 위치까지
# 바뀌면(주변 레이아웃이 달라져 크롭 여백이 달라짐) 이미지 유사도가 0.887
# 까지 떨어지는 반면, 완전히 다른 두 그림의 이미지 유사도가 0.96까지 나오는
# 경우도 있어 임계값 하나로 구분이 안 된다. 캡션 문구는 같은 그림이면 1.0,
# 다른 그림이면 0.05~0.09로 뚜렷이 갈렸다(번호 재배치 시 설명 문구는 저자가
# 그대로 재사용하는 경우가 많기 때문). 그래서 캡션이 있으면 그쪽을 쓰고,
# 캡션이 없거나 너무 짧을 때만(우연히 같을 위험이 있는 만큼) 이미지로 대신한다.
_CAPTION_MIN_LEN = 8
_CAPTION_MATCH_THRESHOLD = 0.7
_FIGURE_MATCH_THRESHOLD = 0.95


def _media_similarity(before_captions: dict[str, str], after_captions: dict[str, str],
                      before_sigs: dict[str, bytes], after_sigs: dict[str, bytes],
                      bl: str, al: str) -> tuple[float, float]:
    """(유사도, 이 유사도에 적용할 통과 기준)을 돌려준다."""
    cb, ca = before_captions.get(bl, ""), after_captions.get(al, "")
    if len(cb) >= _CAPTION_MIN_LEN and len(ca) >= _CAPTION_MIN_LEN:
        return difflib.SequenceMatcher(None, cb, ca).ratio(), _CAPTION_MATCH_THRESHOLD
    return image_similarity(before_sigs.get(bl, b""), after_sigs.get(al, b"")), _FIGURE_MATCH_THRESHOLD


def _rematch_media_labels(prefix: str, before_images: dict[str, bytes], after_images: dict[str, bytes],
                          before_sigs: dict[str, bytes], after_sigs: dict[str, bytes],
                          before_captions: dict[str, str], after_captions: dict[str, str]
                          ) -> dict[str, str]:
    """저자가 그림/표/알고리즘 번호를 재배치했을 때 라벨이 같아도 실제로는
    다른 그림인 경우를 잡아 다시 짝짓는다 — {before_label: after_label}.

    실측: 리비전 사이에 저자가 Figure 5·6 순서를 바꿔서, v2의 "Figure 6"
    (baseline 비교 그리드)이 내용 그대로 v3에서는 "Figure 5"가 되고, v2의
    원래 "Figure 5"(실패 사례)는 다른 번호(Figure 7)로 밀렸다. 라벨만 보고
    짝지으면 완전히 다른 두 그림을 "수정 전/후"로 잘못 비교하게 된다.

    같은 라벨이라고 무조건 먼저 확정하지 않는다 — 두 그림이 공통 문구가
    많은 캡션(예: 같은 실험 템플릿을 반복 서술)을 쓰면 "같은 라벨"인데도
    실제로는 다른 그림인 조합이 threshold를 우연히 넘는 경우가 있고, 그걸
    먼저 확정해버리면 진짜 재배치(더 높은 유사도의 다른 라벨 조합)를 가릴
    수 있다. 그래서 라벨 일치 여부와 무관하게 모든 (before, after) 조합의
    유사도를 구해 threshold를 넘는 것만 후보로 모으고, 전체를 유사도
    내림차순으로 정렬해 가장 비슷한 조합부터 순서대로 확정한다(동점이면
    같은 라벨을 우선한다 — 불필요한 재배치를 피한다).

    그렇게 하고도 threshold를 넘는 상대가 아예 없는 라벨은(재배치가 아니라
    내용 자체가 많이 바뀐 경우), 같은 라벨이 반대편에도 있고 아직 다른
    라벨에게 안 뺏겼다면 원래 라벨 그대로 되돌린다 — 완전히 안 보여주는
    것(삭제+추가로 이중 표시)보다 낫다. 진짜 재배치는 위 유사도 매칭에서
    이미 우선적으로 처리됐으므로 이 fallback과 충돌하지 않는다.
    """
    labels_before = {lbl for lbl in before_images if lbl.startswith(prefix)}
    labels_after = {lbl for lbl in after_images if lbl.startswith(prefix)}

    def sim(bl: str, al: str) -> tuple[float, float]:
        return _media_similarity(before_captions, after_captions, before_sigs, after_sigs, bl, al)

    candidates = []
    for bl in labels_before:
        for al in labels_after:
            ratio, threshold = sim(bl, al)
            if ratio >= threshold:
                candidates.append((ratio, bl, al))
    candidates.sort(key=lambda x: (-x[0], 0 if x[1] == x[2] else 1, x[1], x[2]))

    matched: dict[str, str] = {}
    used_after: set[str] = set()
    for ratio, bl, al in candidates:
        if bl in matched or al in used_after:
            continue
        matched[bl] = al
        used_after.add(al)

    for lbl in labels_before & labels_after:
        if lbl not in matched and lbl not in used_after:
            matched[lbl] = lbl
            used_after.add(lbl)
    return matched


def attach_body_diffs(paper_revisions: PaperRevisions,
                      fetch_version: Callable[[str], "_VersionExtract | None"] | None = None
                      ) -> None:
    """pdf가 교체된 지점마다 본문 전체 diff·그림·표 비교를 끼워 넣는다.

    제자리 수정(paper_revisions.revisions를 직접 바꾼다)이며 반환값은 없다.
    한 transition에서 여러 FieldChange가 나올 수 있다 — field="body"(본문
    텍스트, 항상), field="figure"(그림마다, kind="image"), field="table"
    (표마다, kind="table" — 완전히 같으면 안 만든다).

    fetch_version을 주입 가능하게 둔 건 테스트를 위해서다. 이 모듈의 다른
    테스트는 전부 네트워크·PDF 없는 순수 함수 테스트이므로, 실제
    다운로드+PyMuPDF 대신 URL→_VersionExtract 매핑을 흉내 낸 가짜 함수로
    오케스트레이션(중복 다운로드 방지, skip 조건, 삽입 위치)만 검증한다.
    인자를 생략하면 실제 네트워크 경로(_default_fetch_version)를 쓴다.

    ⚠️ fetch_version은 **여러 스레드에서 동시에 호출된다**(_prefetch_versions).
    URL 하나만 보고 결과를 만드는 함수여야 하며, 공유 상태를 건드린다면
    그쪽에서 직접 보호해야 한다.
    """
    fetch = fetch_version or _default_fetch_version

    # 필요한 PDF를 **아래 루프에 들어가기 전에 한꺼번에** 가져온다. 루프
    # 안에서 그때그때 가져오면 버전 수만큼 다운로드와 추출이 그대로 직렬로
    # 쌓인다(v1→v2→v3면 셋을 순서대로) — 버전끼리는 서로의 결과를 전혀
    # 참조하지 않으므로 동시에 처리해도 결과가 달라지지 않는다.
    # 캐시(dict)는 그대로 남긴다: 같은 URL이 앞 리비전의 after와 다음
    # 리비전의 before로 두 번 등장하는 흔한 경우를 여전히 한 번만 가져오고,
    # 혹시 미리 못 모은 URL이 있어도 예전처럼 그 자리에서 가져온다.
    urls = _pdf_urls(paper_revisions)
    cache: dict[str, "_VersionExtract | None"] = {}
    if len(urls) > 1:
        if fetch is _default_fetch_version:
            # 다운로드 스레드를 띄우기 전에 클라이언트 생성·로그인을 끝내
            # 둔다 — get_client는 첫 호출에서 세션을 만들고 /login까지 하는데,
            # 여러 스레드가 동시에 처음 부르면 로그인이 중복으로 나가고
            # 세션 헤더(Authorization)를 서로 덮어쓴다.
            get_client(V2)
        cache.update(_prefetch_versions(urls, fetch))

    def _version_for(url: str | None) -> "_VersionExtract | None":
        if not url:
            return None
        if url not in cache:
            cache[url] = fetch(url)
        return cache[url]

    # v1(baseline) 자체는 "pdf" FieldChange가 없어(비교할 이전 버전이 없으니
    # 당연하다) 아래 루프가 절대 baseline entry를 직접 방문하지 않는다. 그래서
    # 프론트는 지금까지 "v1 원문"을 첫 transition(v1→v2)의 diff segments에서
    # insert만 뺀 "before" 재구성으로 대신 만들어 왔다(App.jsx 주석 참고) —
    # 그런데 그 세그먼트 리스트는 **v2 쪽 위치를 우선**하도록 설계돼 있어서
    # (moved 문단은 v2에서의 자리를 그대로 쓴다, _match_moved_paragraphs 참고),
    # v1 자신을 보여줄 때는 문단·그림 순서가 실제 v1 원본과 어긋난다(실측:
    # "(그림 2)"가 v1 원본에서는 "Pixel-level encoder" 문단 바로 뒤인데,
    # "Cross-task label encoder" 문단이 v2 쪽 자리로 이동해 그 사이에 끼어들며
    # 순서가 뒤집힘). v1은 diff 색칠이 필요 없는 "원문 그대로" 화면이므로,
    # 재사용하지 않고 v1 자신의 문단 순서(_paragraphs(before.text))로 별도의
    # all-equal segments를 만들어 baseline entry에 직접 붙인다.
    baseline_entry = (paper_revisions.revisions[0]
                      if paper_revisions.revisions and paper_revisions.revisions[0].is_baseline
                      else None)
    baseline_body_attached = False

    for entry in paper_revisions.revisions:
        offset = 0
        for original_index, change in enumerate(list(entry.changes)):
            if change.field != "pdf" or change.kind != "file":
                continue
            before = _version_for(change.before_url)
            after = _version_for(change.after_url)
            if (baseline_entry is not None and not baseline_body_attached
                    and entry is not baseline_entry and before and before.text):
                baseline_segs = [DiffSegment(op="equal", text=p + "\n\n") for p in _paragraphs(before.text)]
                baseline_entry.changes.append(FieldChange(
                    field="body", label="본문", kind="text", similarity=1.0, segments=baseline_segs))
                baseline_body_attached = True
            if not before or not after or not before.text or not after.text:
                continue

            new_changes: list[FieldChange] = []

            # 그림·표·알고리즘·박스 모두 "이미지로 전/후 비교"라는 같은 모양이라
            # 같은 방식으로 처리한다. 완전히 같은지는 픽셀 비교 없이는 알 수
            # 없으므로(그림과 동일한 판단), 한쪽에만 있든 양쪽에 다 있든 항상
            # 보여준다. 박스는 내용 해시가 라벨이라 완전히 같으면 애초에 같은
            # 라벨로 잡히므로(반영 안 함) before-only/after-only만 유사도로
            # 짝짓는다(_match_boxes). 그림·표·알고리즘은 저자 번호가 라벨이라
            # 같은 라벨이 양쪽에 다 있어도 내용이 다를 수 있어(번호 재배치,
            # _rematch_media_labels) 판단 순서가 다르다.
            # 라벨이 재배치되면(예: v2 Figure 6 → v3 Figure 5, 동시에 v2 Figure 7 →
            # v3 Figure 6) "Figure 6"라는 문자열을 한쪽은 수정 전 번호로, 다른
            # 쪽은 수정 후 번호로 동시에 주장하는 충돌이 생긴다. 평평한
            # "라벨 하나 = 이미지 하나" 표로는 이 충돌을 표현할 수 없어(실측:
            # 두 번째로 등록된 쪽이 첫 번째를 덮어써 서로 다른 두 그림이 뒤섞임)
            # FieldChange마다 label(수정 전 번호)과 after_label(수정 후 번호,
            # 다를 때만)을 따로 갖게 한다 — 본문 쪽 "(그림 N)" 자리표시자도
            # v2 문장은 항상 label 쪽 번호를, v3 문장은 항상 after_label 쪽
            # 번호를 그대로 담고 있으므로 프론트가 문맥(수정 전/후 중 어느 쪽
            # 텍스트를 그리는지)에 따라 맞는 필드로 찾으면 충돌이 안 생긴다.
            #
            # 본문 diff보다 먼저 계산해 둔다 — 번호가 재배치된 쌍(bl != al)을
            # 알아야 본문 자리표시자 중 "(그림 5)"(delete)와 "(그림 7)"(insert)
            # 처럼 번호 자체가 다른 것도 moved로 합칠 수 있다
            # (_merge_media_placeholders, 실측: 안 그러면 재배치된 그림은
            # "위치 이동됨" 표시 없이 그냥 삭제+추가로 보임).
            all_matches: dict[str, dict[str, str]] = {}
            for prefix in ("Figure", "Table", "Algorithm", "Box", "Equation"):
                if prefix == "Box":
                    matches = _match_boxes(
                        [lbl for lbl in before.images if lbl.startswith("Box") and lbl not in after.images],
                        [lbl for lbl in after.images if lbl.startswith("Box") and lbl not in before.images],
                        before.box_texts, after.box_texts, before.images, after.images)
                    matches.update({lbl: lbl for lbl in before.images
                                    if lbl.startswith("Box") and lbl in after.images})
                elif prefix == "Equation":
                    # 수식은 캡션 문구가 없어(번호뿐) Figure/Table처럼 설명
                    # 텍스트로 재배치를 재매칭할 방법이 없다 — 같은 번호가
                    # 양쪽에 다 있을 때만 "같은 수식"으로 본다. 번호가
                    # 바뀌면 삭제+삽입으로 보인다(Figure에 캡션 기반 재매칭을
                    # 넣기 전과 같은 수준 — 정교한 매칭은 나중 과제로 남긴다).
                    matches = {lbl: lbl for lbl in before.images
                              if lbl.startswith("Equation") and lbl in after.images}
                else:
                    matches = _rematch_media_labels(
                        prefix, before.images, after.images, before.signatures, after.signatures,
                        before.captions, after.captions)
                all_matches[prefix] = matches
            renumbered = {bl: al for matches in all_matches.values()
                         for bl, al in matches.items() if bl != al}

            ratio, segs = _paragraph_diff(before.text, after.text, renumbered)
            new_changes.append(FieldChange(
                field="body", label="본문", kind="text", similarity=ratio, segments=segs))

            for prefix, field in (("Figure", "figure"), ("Table", "table"), ("Algorithm", "algorithm"),
                                  ("Box", "box"), ("Equation", "equation")):
                matches = all_matches[prefix]
                prefix_before = {lbl for lbl in before.images if lbl.startswith(prefix)}
                prefix_after = {lbl for lbl in after.images if lbl.startswith(prefix)}

                for bl in _numbered(matches.keys()):
                    al = matches[bl]
                    new_changes.append(FieldChange(
                        field=field, label=bl, after_label=(al if al != bl else None), kind="image",
                        before_image=_data_uri(before.images.get(bl)),
                        after_image=_data_uri(after.images.get(al))))
                for bl in _numbered(prefix_before - set(matches.keys())):
                    new_changes.append(FieldChange(
                        field=field, label=bl, kind="image",
                        before_image=_data_uri(before.images.get(bl)), after_image=None))
                for al in _numbered(prefix_after - set(matches.values())):
                    new_changes.append(FieldChange(
                        field=field, label=al, kind="image",
                        before_image=None, after_image=_data_uri(after.images.get(al))))

            # 같은 transition의 파일 이벤트 바로 다음에 끼워 넣는다 — 프론트가
            # 어느 diff가 어느 pdf 교체에 속하는지 찾아 헤매지 않게.
            for i, nc in enumerate(new_changes):
                entry.changes.insert(original_index + 1 + offset + i, nc)
            offset += len(new_changes)


def _load_body_cache(paper_id: int) -> PaperRevisions | None:
    try:
        with cursor() as cur:
            cur.execute(_BODY_CACHE_GET_SQL, (paper_id,))
            row = cur.fetchone()
    except Exception as exc:     # 테이블 미생성(alembic 미적용) 등
        log.warning("본문 diff 캐시 조회 실패 — 캐시 없이 진행합니다: %s", exc)
        return None
    if row is None:
        return None

    payload, built_at = row
    if datetime.now(timezone.utc) - built_at > STALE_AFTER:
        return None

    try:
        revisions = PaperRevisions.model_validate(payload)
    except Exception as exc:
        # PaperRevisions가 바뀌면 옛 payload는 못 읽는다. 버리고 다시 만든다.
        log.info("본문 diff 캐시 형식이 맞지 않아 다시 만듭니다 (paper_id=%s): %s",
                 paper_id, exc)
        return None
    revisions.cached_at = built_at.isoformat()
    return revisions


def _store_body_cache(paper_id: int, revisions: PaperRevisions) -> None:
    try:
        with cursor(commit=True) as cur:
            cur.execute(_BODY_CACHE_PUT_SQL,
                        (paper_id, Jsonb(revisions.model_dump(mode="json"))))
    except Exception as exc:
        # 캐시는 성능 장치일 뿐이라 저장 실패로 응답을 버리지 않는다.
        log.warning("본문 diff 캐시 저장 실패 (paper_id=%s): %s", paper_id, exc)


def get_paper_revisions_with_body(paper_id: int, refresh: bool = False) -> PaperRevisions | None:
    """paper_id의 리비전 이력 + 각 pdf 교체 지점의 본문 전체 diff.

    get_paper_revisions보다 훨씬 비싸다 — pdf가 바뀐 지점마다 전후 PDF를 실제로
    내려받아 텍스트를 뽑고 diff한다. 그래서 결과를 paper_body_diffs에 캐시한다
    (story.py와 같은 판단: 심사가 끝난 논문은 리비전도 본문도 더 안 바뀌므로
    사실상 영구 캐시). refresh=True면 캐시를 무시하고 다시 만든다.
    """
    if not refresh:
        cached = _load_body_cache(paper_id)
        if cached is not None:
            return cached

    revisions = get_paper_revisions(paper_id)
    if revisions is None:
        return None
    if revisions.supported and revisions.revisions:
        attach_body_diffs(revisions)
    # 외부 조회가 실패한 결과(supported=False)는 캐시하지 않는다 — 일시적 장애를
    # 30일 동안 붙잡아두면 다음 요청들이 전부 '볼 수 없음'을 받는다.
    if revisions.supported:
        _store_body_cache(paper_id, revisions)
    return revisions
