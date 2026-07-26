"""수정 이력 순수 로직 테스트 (DB·네트워크 불필요).

여기서 고정하는 건 전부 OpenReview API 실측에서 나온 사항이다 — 값이 {"value": ...}로
감싸여 오고, 삭제는 한 겹 더 들어간 {"value": {"delete": true}}이고, edit이 전체
스냅샷이 아니라 부분 패치라는 것.
"""
from paper_assistant.revisions import _classify, _diff_fields, _unwrap, _word_diff


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
