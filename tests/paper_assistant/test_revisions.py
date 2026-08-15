"""수정 이력 순수 로직 테스트 (DB·네트워크 불필요).

여기서 고정하는 건 전부 OpenReview API 실측에서 나온 사항이다 — 값이 {"value": ...}로
감싸여 오고, 삭제는 한 겹 더 들어간 {"value": {"delete": true}}이고, edit이 전체
스냅샷이 아니라 부분 패치라는 것.
"""
import threading

import pytest

from paper_assistant.pdf.extract import BodyExtract
from paper_assistant.query import revisions as R
from paper_assistant.query.revisions import (
    _classify, _diff_fields, _rematch_media_labels, _unwrap, _word_diff, attach_body_diffs,
    _MAX_DOWNLOAD_WORKERS, _prefetch_default, _version_from_body, _VersionExtract)
from paper_assistant.schemas import FieldChange, PaperRevisions, RevisionEntry


def diff(prev, cur, prev_src=None, cur_src=None):
    """파일 출처(edit id)는 대부분의 테스트와 무관하므로 기본값을 준다."""
    return _diff_fields(prev, cur, prev_src or {}, cur_src or {})


def test_unwrap_pulls_value():
    assert _unwrap({"value": "hello"}) == "hello"
    assert _unwrap("bare") == "bare"
    assert _unwrap(None) is None


def test_unwrap_detects_nested_delete_sentinel():
    # 실측 형태. 한 겹만 벗기면 "{'delete': True}"가 초록 diff에 문자열로 찍힌다.
    assert _unwrap({"value": {"delete": True}}) is None
    assert _unwrap({"delete": True}) is None


def test_unwrap_joins_list_values():
    assert _unwrap({"value": ["graph", "dropout"]}) == "graph, dropout"


def test_classify_camera_ready_not_swallowed_by_revision():
    # 'Revision'이 'Camera_Ready_Revision'의 부분 문자열이라 순서가 중요하다
    assert _classify("ICLR.cc/2025/Conference/Submission1/-/Camera_Ready_Revision") == (
        "camera_ready", "게재 확정본")
    assert _classify("ICLR.cc/2025/Conference/Submission1/-/Rebuttal_Revision") == (
        "rebuttal", "리뷰 반영 수정")
    assert _classify("X/-/Revision")[0] == "revision"


def test_classify_unknown_invitation_falls_back():
    kind, label = _classify("ICLR.cc/2024/Conference/Submission1/-/Some_New_Thing")
    assert kind == "other"
    assert label == "Some New Thing"


def test_word_diff_marks_only_changed_words():
    ratio, segs = _word_diff("across 16 datasets total", "across 14 datasets total")
    assert ratio > 0.7
    assert [(s.op, s.text) for s in segs if s.op != "equal"] == [
        ("delete", "16"), ("insert", "14")]


def test_diff_fields_skips_first_appearance():
    """앞 버전에 없던 필드는 '추가'로 단정하지 않는다.

    edit이 부분 패치라, 필드가 늦게 나타난 건 그때 생겼다는 뜻이 아니라 그전
    edit에 안 실렸다는 뜻일 뿐이다.
    """
    assert diff({"abstract": "a b c"}, {"abstract": "a b c", "title": "T"}) == []


def test_diff_fields_reports_text_change_with_segments():
    changes = diff({"abstract": "a b c"}, {"abstract": "a x c"})
    assert len(changes) == 1
    c = changes[0]
    assert (c.field, c.label, c.kind) == ("abstract", "초록", "text")
    assert c.similarity is not None
    assert any(s.op == "insert" for s in c.segments)


def test_diff_fields_describes_files_without_hashes():
    """PDF는 경로 해시만 바뀌므로 해시를 노출하지 않고 무슨 일인지만 말한다."""
    (c,) = diff({"pdf": "/pdf/aaa.pdf"}, {"pdf": "/pdf/bbb.pdf"})
    assert (c.kind, c.after) == ("file", "교체됨")
    (c,) = diff({"pdf": "/pdf/aaa.pdf"}, {"pdf": None})
    assert c.after == "삭제됨"


def test_diff_fields_links_both_pdf_versions_by_edit_id():
    """전/후 링크는 값이 아니라 그 파일을 실어 나른 edit id에서 나온다.

    content의 /pdf/<해시>.pdf는 교체되는 순간 404라 링크로 쓸 수 없다.
    """
    (c,) = diff({"pdf": "/pdf/aaa.pdf"}, {"pdf": "/pdf/bbb.pdf"},
                {"pdf": "editOLD"}, {"pdf": "editNEW"})
    assert "editOLD" in c.before_url
    assert "editNEW" in c.after_url
    assert "name=pdf" in c.after_url


def test_diff_fields_omits_link_for_missing_side():
    """삭제된 쪽에는 받을 파일이 없으므로 링크를 만들지 않는다."""
    (c,) = diff({"pdf": "/pdf/aaa.pdf"}, {"pdf": None}, {"pdf": "editOLD"}, {})
    assert c.before_url and c.after_url is None
    (c,) = diff({"pdf": None}, {"pdf": "/pdf/bbb.pdf"}, {}, {"pdf": "editNEW"})
    assert c.before_url is None and c.after_url


def test_diff_fields_marks_deleted_value_as_none():
    """삭제된 값은 after=None으로 둔다 — 프론트가 '삭제됨'으로 그린다."""
    (c,) = diff({"TLDR": "one liner"}, {"TLDR": None})
    assert c.kind == "value" and c.after is None


def test_diff_fields_ignores_untracked_noise():
    """_bibtex·venue 같은 시스템 생성 필드는 저자 수정이 아니라 노이즈다."""
    assert diff({"_bibtex": "@x{}"}, {"_bibtex": "@y{}"}) == []


# ---------------------------------------------------------- attach_body_diffs
#
# 실제 다운로드(get_bytes)+PyMuPDF는 여기서 테스트하지 않는다 — 이 파일의 다른
# 테스트처럼 순수 함수 컨벤션을 지키기 위해 fetch_version을 URL→_VersionExtract
# 가짜 함수로 주입해서, 오케스트레이션(중복 제거·skip 조건·삽입 위치·행 매칭)만
# 검증한다.

def _pdf_change(before_url="u://before.pdf", after_url="u://after.pdf"):
    return FieldChange(field="pdf", label="본문 PDF", kind="file", after="교체됨",
                       before_url=before_url, after_url=after_url)


def _revisions_with(*changes_lists):
    entries = [
        RevisionEntry(revision_id=f"r{i}", kind="revision", kind_label="수정본",
                      timestamp=i, date="2026-01-01 00:00", changes=list(changes))
        for i, changes in enumerate(changes_lists)
    ]
    return PaperRevisions(paper_id=1, openreview_id="abc", supported=True,
                          revisions=entries)


def _version(text, images=None):
    return _VersionExtract(text=text, images=images or {})


def test_attach_body_diffs_inserts_body_change_right_after_pdf_change():
    revisions = _revisions_with([_pdf_change()])
    versions = {"u://before.pdf": _version("the quick brown fox"),
               "u://after.pdf": _version("the quick red fox")}

    attach_body_diffs(revisions, fetch_version=versions.get)

    changes = revisions.revisions[0].changes
    assert [c.field for c in changes] == ["pdf", "body"]
    body = changes[1]
    assert body.kind == "text"
    assert body.similarity is not None
    assert ("delete", "brown") in [(s.op, s.text) for s in body.segments]
    assert ("insert", "red") in [(s.op, s.text) for s in body.segments]


def test_attach_body_diffs_does_not_store_full_text():
    """저장 공간을 아끼려고 before/after는 채우지 않는다 — segments만으로 재구성."""
    revisions = _revisions_with([_pdf_change()])
    versions = {"u://before.pdf": _version("a b c"), "u://after.pdf": _version("a x c")}

    attach_body_diffs(revisions, fetch_version=versions.get)

    body = revisions.revisions[0].changes[1]
    assert body.before is None and body.after is None


def test_attach_body_diffs_inserts_immediately_after_pdf_not_at_end():
    """리비전에 다른 필드 변경도 있으면, body는 끝이 아니라 pdf 바로 다음에 온다."""
    title_change = FieldChange(field="title", label="제목", kind="text",
                               before="A", after="B", similarity=0.5, segments=[])
    revisions = _revisions_with([title_change, _pdf_change()])
    versions = {"u://before.pdf": _version("a b"), "u://after.pdf": _version("a c")}

    attach_body_diffs(revisions, fetch_version=versions.get)

    assert [c.field for c in revisions.revisions[0].changes] == ["title", "pdf", "body"]


def test_attach_body_diffs_dedupes_downloads_across_revisions():
    """리비전1의 after가 리비전2의 before로 재등장 — 같은 URL은 한 번만 가져온다."""
    revisions = _revisions_with(
        [_pdf_change("u://a", "u://b")],
        [_pdf_change("u://b", "u://c")],
    )
    calls: list[str] = []
    versions = {"u://a": _version("one two three"), "u://b": _version("one two four"),
               "u://c": _version("one five four")}

    def fake_fetch(url):
        calls.append(url)
        return versions[url]

    attach_body_diffs(revisions, fetch_version=fake_fetch)

    assert calls.count("u://b") == 1
    assert sorted(set(calls)) == ["u://a", "u://b", "u://c"]


def test_attach_body_diffs_fetches_versions_concurrently():
    """버전별 PDF는 동시에 가져온다 — 하나씩 순서대로 기다리지 않는다.

    "빨라졌는지"를 시간으로 재면 CI에서 불안정하다. 대신 fetch 안에서 서로를
    기다리게(Barrier) 만들어 **동시에 돌지 않으면 아예 끝나지 않도록** 한다 —
    순차로 돌아가면 첫 호출이 barrier에서 시간 초과로 터진다.
    """
    revisions = _revisions_with([_pdf_change("u://a", "u://b")])
    barrier = threading.Barrier(2, timeout=5)

    def fake_fetch(url):
        barrier.wait()   # 두 fetch가 동시에 살아 있어야만 통과한다
        return _version(f"body of {url}")

    attach_body_diffs(revisions, fetch_version=fake_fetch)

    assert [c.field for c in revisions.revisions[0].changes] == ["pdf", "body"]


def test_attach_body_diffs_limits_concurrent_fetches():
    """동시 다운로드 수에 상한을 둔다 — OpenReview가 429로 막지 않도록."""
    urls = [f"u://v{i}" for i in range(_MAX_DOWNLOAD_WORKERS + 4)]
    revisions = _revisions_with(*[[_pdf_change(a, b)] for a, b in zip(urls, urls[1:])])

    lock = threading.Lock()
    live = peak = 0

    def fake_fetch(url):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        try:
            return _version(f"body of {url}")
        finally:
            with lock:
                live -= 1

    attach_body_diffs(revisions, fetch_version=fake_fetch)

    assert peak <= _MAX_DOWNLOAD_WORKERS


# ----------------------------------------------- 실제 경로(다운로드/추출 분리)
#
# 실제 경로는 다운로드(스레드)와 추출(프로세스)을 나눠 돌린다. 여기서는
# 프로세스를 띄우지 않고 — _parse_executor를 스레드 실행기로 바꿔 끼워 —
# 그 분리와 결과 조립만 검증한다. "정말 프로세스로 도는가"는 코드가 아니라
# 성능의 문제라 단위 테스트가 아니라 실측으로 확인했다(_MAX_PARSE_WORKERS 주석).

def _body(text, images=None):
    return BodyExtract(text=text, images=images or {}, box_texts={}, captions={},
                       signatures={})


@pytest.fixture
def parse_in_threads(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(R, "_parse_executor", lambda workers: ThreadPoolExecutor(workers))


def test_prefetch_default_downloads_then_extracts(parse_in_threads, monkeypatch):
    """URL마다 다운로드 1회 + 추출 1회, 결과는 URL에 제대로 짝지어진다."""
    pdfs = {"u://a": b"PDF-A", "u://b": b"PDF-B"}
    monkeypatch.setattr(R, "_download_pdf", lambda url: pdfs[url])
    monkeypatch.setattr(R, "extract_body",
                        lambda data, max_pages=None: _body(f"body of {data.decode()}"))

    out = _prefetch_default(["u://a", "u://b"])

    assert out["u://a"].text == "body of PDF-A"
    assert out["u://b"].text == "body of PDF-B"


def test_prefetch_default_skips_dead_links_but_keeps_the_rest(parse_in_threads, monkeypatch):
    """죽은 링크(404)는 None으로 남기고 나머지는 그대로 추출한다 — 추출에도 안 보낸다."""
    monkeypatch.setattr(R, "_download_pdf", lambda url: None if url == "u://dead" else b"PDF")
    extracted = []

    def fake_extract(data, max_pages=None):
        extracted.append(data)
        return _body("body")

    monkeypatch.setattr(R, "extract_body", fake_extract)

    out = _prefetch_default(["u://dead", "u://ok"])

    assert out["u://dead"] is None
    assert out["u://ok"].text == "body"
    assert len(extracted) == 1


def test_prefetch_default_falls_back_when_processes_are_unavailable(monkeypatch):
    """프로세스를 못 띄우는 환경(권한이 막힌 컨테이너 등)에서는 이 프로세스에서 처리한다."""
    monkeypatch.setattr(R, "_download_pdf", lambda url: b"PDF")
    monkeypatch.setattr(R, "extract_body", lambda data, max_pages=None: _body("body"))

    def no_processes(workers):
        raise OSError("프로세스를 만들 수 없음")

    monkeypatch.setattr(R, "_parse_executor", no_processes)

    out = _prefetch_default(["u://a"])

    assert out["u://a"].text == "body"


def test_version_from_body_rejects_unusable_extractions():
    """페이지 수 상한 초과(text=None)와 깨진 텍스트는 '비교 불가'로 걸러낸다."""
    assert _version_from_body(_body(None)) is None
    assert _version_from_body(_body("ᔥ ᖇ ᒍ " * 30)) is None
    good = _version_from_body(_body("a perfectly ordinary sentence of body text"))
    assert good is not None and good.text.startswith("a perfectly")


def test_attach_body_diffs_propagates_fetch_errors():
    """fetch가 던진 예외는 삼키지 않는다 — 순차로 가져오던 때와 같은 동작."""
    revisions = _revisions_with([_pdf_change("u://a", "u://b")])

    def boom(url):
        raise RuntimeError("PDF 파싱 실패")

    with pytest.raises(RuntimeError, match="PDF 파싱 실패"):
        attach_body_diffs(revisions, fetch_version=boom)


def test_attach_body_diffs_skips_when_url_missing():
    """pdf가 추가만 됐거나 삭제만 된 경우 (한쪽 링크가 없음) — 본문 diff를 만들지 않는다."""
    revisions = _revisions_with([_pdf_change(before_url=None)])
    attach_body_diffs(revisions, fetch_version=lambda url: _version("text"))
    assert [c.field for c in revisions.revisions[0].changes] == ["pdf"]


def test_attach_body_diffs_skips_when_fetch_fails():
    """다운로드 실패·깨진 텍스트는 fetch_version이 None으로 알려온다 — 조용히 건너뛴다."""
    revisions = _revisions_with([_pdf_change()])
    attach_body_diffs(revisions, fetch_version=lambda url: None)
    assert [c.field for c in revisions.revisions[0].changes] == ["pdf"]


def test_attach_body_diffs_ignores_non_pdf_file_changes():
    """supplementary_material도 kind=='file'이지만 PDF가 아닐 수 있어 대상에서 뺀다."""
    supplementary = FieldChange(field="supplementary_material", label="보충 자료",
                                kind="file", after="교체됨",
                                before_url="u://s1", after_url="u://s2")
    revisions = _revisions_with([supplementary])
    attach_body_diffs(revisions, fetch_version=lambda url: _version("text"))
    assert [c.field for c in revisions.revisions[0].changes] == ["supplementary_material"]


# ------------------------------------------------------- 그림/표 (attach_body_diffs)

def test_attach_body_diffs_adds_figure_as_image_kind_no_highlight():
    """그림은 하이라이트 없이 전/후 이미지만 나란히 붙인다."""
    revisions = _revisions_with([_pdf_change()])
    versions = {
        "u://before.pdf": _version("t1", images={"Figure 1": b"OLDPNG"}),
        "u://after.pdf": _version("t2", images={"Figure 1": b"NEWPNG"}),
    }

    attach_body_diffs(revisions, fetch_version=versions.get)

    changes = revisions.revisions[0].changes
    assert [c.field for c in changes] == ["pdf", "body", "figure"]
    fig = changes[2]
    assert fig.kind == "image" and fig.label == "Figure 1"
    assert fig.before_image.startswith("data:image/png;base64,")
    assert fig.after_image.startswith("data:image/png;base64,")
    assert fig.segments == []  # 그림엔 diff segment가 없다


def test_attach_body_diffs_figure_only_on_one_side_is_still_shown():
    """새로 추가되거나 삭제된 그림도 (없는 쪽은 None으로) 보여준다."""
    revisions = _revisions_with([_pdf_change()])
    versions = {
        "u://before.pdf": _version("t1", images={}),
        "u://after.pdf": _version("t2", images={"Figure 3": b"NEWPNG"}),
    }

    attach_body_diffs(revisions, fetch_version=versions.get)

    fig = revisions.revisions[0].changes[2]
    assert fig.label == "Figure 3"
    assert fig.before_image is None
    assert fig.after_image.startswith("data:image/png;base64,")


def test_attach_body_diffs_table_is_treated_as_image_like_a_figure():
    """표도 그림과 똑같이 kind='image'로, field만 'table'로 구분해서 붙인다.

    find_tables()로 셀 구조까지 diff하는 건 병합된 셀 때문에 원본과 많이
    달라져서 포기했다 — 표도 영역을 그대로 이미지로 잘라 비교한다.
    """
    revisions = _revisions_with([_pdf_change()])
    versions = {
        "u://before.pdf": _version("t1", images={"Table 1": b"OLDPNG"}),
        "u://after.pdf": _version("t2", images={"Table 1": b"NEWPNG"}),
    }

    attach_body_diffs(revisions, fetch_version=versions.get)

    changes = revisions.revisions[0].changes
    assert [c.field for c in changes] == ["pdf", "body", "table"]
    table = changes[2]
    assert table.kind == "image" and table.label == "Table 1"
    assert table.before_image.startswith("data:image/png;base64,")
    assert table.after_image.startswith("data:image/png;base64,")


def test_attach_body_diffs_figures_and_tables_both_appear_sorted_by_number():
    """그림·표가 섞여 있어도 둘 다 붙는다 — 정렬은 종류 구분 없이 번호로만 한다
    (field로 그림/표를 구분하는 건 위치가 아니라 프론트가 본문 안 자리표시자로
    한다)."""
    revisions = _revisions_with([_pdf_change()])
    versions = {
        "u://before.pdf": _version("t1", images={"Table 2": b"T2", "Figure 1": b"F1"}),
        "u://after.pdf": _version("t2", images={"Table 2": b"T2b", "Figure 1": b"F1b"}),
    }

    attach_body_diffs(revisions, fetch_version=versions.get)

    changes = revisions.revisions[0].changes
    labels = [(c.field, c.label) for c in changes if c.kind == "image"]
    assert labels == [("figure", "Figure 1"), ("table", "Table 2")]


# ------------------------------------------------ 표 번호 재배치 (합성 케이스)
#
# 실제 논문으로 재현을 시도했으나(26079 등 여러 편) 표 번호가 리비전 사이에
# 재배치되는 실측 사례를 못 찾았다 — Figure 5·6·7 재배치처럼 흔하지 않다.
# _rematch_media_labels는 prefix만 다를 뿐 Figure와 완전히 같은 함수를
# 공유하므로(paper_assistant/query/revisions.py), Figure 5·6·7 실측으로 이미
# 검증된 로직 그대로다 — 합성 데이터로 표에도 동작을 고정해 둔다.

def test_rematch_media_labels_finds_renumbered_table_by_caption():
    """표 5가 삭제되면서 표 6이 표 5로 당겨진 경우 — 캡션으로 진짜 상대를 찾는다."""
    before_captions = {
        "Table 5": "Ablation study on the effect of the proposed regularization term",
        "Table 6": "Comparison with baseline methods on the held-out test set",
    }
    after_captions = {
        "Table 5": "Comparison with baseline methods on the held-out test set",
    }
    before_images = {"Table 5": b"old-ablation", "Table 6": b"old-comparison"}
    after_images = {"Table 5": b"new-comparison"}

    matches = _rematch_media_labels(
        "Table", before_images, after_images, {}, {}, before_captions, after_captions)

    assert matches == {"Table 6": "Table 5"}
    assert "Table 5" not in matches  # 진짜 삭제된 표는 매칭되지 않고 남는다


def test_attach_body_diffs_reflects_renumbered_table_with_both_images():
    """재배치된 표는 field='table' 항목 하나로 합쳐지고 양쪽 이미지가 다 있다 —
    번호가 재사용된 삭제 표는 별도 항목으로 before만 채워진다."""
    before = _VersionExtract(
        text="t1", images={"Table 5": b"old-ablation", "Table 6": b"old-comparison"},
        captions={
            "Table 5": "Ablation study on the effect of the proposed regularization term",
            "Table 6": "Comparison with baseline methods on the held-out test set",
        })
    after = _VersionExtract(
        text="t2", images={"Table 5": b"new-comparison"},
        captions={"Table 5": "Comparison with baseline methods on the held-out test set"})
    revisions = _revisions_with([_pdf_change()])

    attach_body_diffs(revisions, fetch_version={"u://before.pdf": before, "u://after.pdf": after}.get)

    tables = {c.label: c for c in revisions.revisions[0].changes if c.field == "table"}
    assert tables["Table 6"].after_label == "Table 5"
    assert tables["Table 6"].before_image is not None
    assert tables["Table 6"].after_image is not None
    assert tables["Table 5"].before_image is not None
    assert tables["Table 5"].after_image is None  # 진짜 삭제됨, 재배치와 안 섞인다
