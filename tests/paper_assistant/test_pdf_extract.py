"""PDF 추출 헬퍼 테스트 (실제 PDF 불필요 — 순수 로직만).

**span에 좌표를 넣는다.** 제목 조립이 좌표로 공백 여부를 판단하기 때문이다
(_join_spans). 좌표 없이 텍스트만 주면 실제 동작과 다른 것을 검증하게 된다.

아래 헬퍼의 간격 값은 실측에서 가져왔다 (LoRA 논문 ICLR 스타일 제목):
단어 **내부** 0.85pt, 단어 **사이** 10.69pt.
"""
from paper_assistant.pdf.extract import (
    _abstract_from_text, _looks_garbled, _title_from_spans)

_PIECE_GAP = 0.85     # 한 단어가 쪼개진 조각 사이
_WORD_GAP = 10.69     # 단어 사이
_LEFT = 108.0


def _spans(*items, page=0):
    """(size, text) 각각을 **한 줄씩** 놓는다 (줄이 바뀌면 공백으로 이어진다)."""
    out, y = [], 80.0
    for sz, t in items:
        out.append((sz, t, page, (_LEFT, y, _LEFT + len(t) * sz * 0.55, y + sz)))
        y += sz * 1.6
    return out


def _line(*words, page=0, y=80.0):
    """한 줄에 나란히 놓는다. 각 word는 (size, text) 조각들의 튜플이다.

    조각 사이는 좁게(같은 단어), 단어 사이는 넓게 벌린다 — 드롭캡 조판 그대로다.
    """
    out, x = [], _LEFT
    for i, pieces in enumerate(words):
        if i:
            x += _WORD_GAP
        for sz, t in pieces:
            width = len(t) * sz * 0.55
            out.append((sz, t, page, (x, y, x + width, y + sz)))
            x += width + _PIECE_GAP
    return out


# ------------------------------------------------------------ 제목 (폰트 크기)

def test_title_excludes_authors_by_font_size():
    """저자가 제목 바로 뒤에 붙어도 폰트 크기로 배제된다 (실측 케이스)."""
    spans = _spans(
        (14.3, "Agentic Business Process Management:"),
        (14.3, "A Research Manifesto"),
        (10.0, "Diego Calvanese"),
        (7.0, "a"),
        (10.0, ", Angelo Casciani"),
    )
    title = _title_from_spans(spans)
    assert title == "Agentic Business Process Management: A Research Manifesto"
    assert "Calvanese" not in title
    assert "Casciani" not in title


def test_title_excludes_affiliation_and_email():
    spans = _spans(
        (17.2, "ImageNet Classification with Deep Convolutional"),
        (17.2, "Neural Networks"),
        (10.0, "Alex Krizhevsky"),
        (10.0, "University of Toronto"),
        (10.0, "kriz@cs.utoronto.ca"),
    )
    title = _title_from_spans(spans)
    assert title == "ImageNet Classification with Deep Convolutional Neural Networks"
    assert "Toronto" not in title and "@" not in title


def test_title_handles_drop_cap_typography():
    """드롭캡: 첫 글자만 큰 폰트여도 제목 전체가 이어져야 한다."""
    spans = (
        _spans((20.0, "arXiv:1409.2329v5  [cs.NE]  19 Feb 2015"),
               (10.0, "Under review as a conference paper at ICLR 2015"))
        + _line(((17.2, "R"), (13.8, "ECURRENT")),
                ((17.2, "N"), (13.8, "EURAL")),
                ((17.2, "N"), (13.8, "ETWORK")),
                ((17.2, "R"), (13.8, "EGULARIZATION")), y=200.0)
        + _spans((10.0, "Wojciech Zaremba"))
    )
    title = _title_from_spans(spans)
    assert title == "RECURRENT NEURAL NETWORK REGULARIZATION"
    assert "arXiv" not in title
    assert "Zaremba" not in title


def test_title_joins_a_word_split_into_three_pieces():
    """정규식으로는 못 고치던 경우 — 실측 실패 사례가 여기였다.

    'L'+'O'+'RA:'처럼 한 단어가 셋으로 쪼개지면 "대문자 1개 + 공백 + 대문자 2자 이상"
    규칙이 걸리지 않아 `L ORA:`가 그대로 남았다.
    """
    spans = _line(((17.2, "L"), (13.8, "O"), (17.2, "RA:")),
                  ((17.2, "L"), (13.8, "OW"), (17.2, "-R"), (13.8, "ANK")))
    assert _title_from_spans(spans) == "LORA: LOW-RANK"


def test_title_rejoins_a_word_broken_by_a_line_break_hyphen():
    """'LAN-' + 줄바꿈 + 'GUAGE' → 'LANGUAGE'. 하이픈을 남기면 단어가 깨진다.

    하이픈이 **자기 span**으로 떨어져 나오는 조판이라, 예전에는 그 span이
    잡동사니 규칙(`^\W*$`)에 걸려 삭제되면서 'LAN GUAGE'가 됐다.
    """
    spans = (_line(((17.2, "L"), (13.8, "AN"), (17.2, "-")), y=80.0)
             + _line(((13.8, "GUAGE"),), y=103.0))
    assert _title_from_spans(spans) == "LANGUAGE"


def test_title_keeps_a_space_that_lives_inside_the_span_text():
    """span 텍스트가 이미 공백으로 시작하면 그게 진짜 띄어쓰기다.

    bbox는 그 공백까지 포함해 잡히므로 좌표로는 '붙어 있음'으로 보인다 —
    strip 해버리면 단어가 붙어 'GUAGEMODELS'가 된다 (실측 사례).
    """
    spans = _line(((13.8, "GUAGE"), (17.2, " M"), (13.8, "ODELS")))
    assert _title_from_spans(spans) == "GUAGE MODELS"


def test_title_separates_words_that_are_far_apart():
    """간격이 넓으면 다른 단어다 — 전부 붙여버리면 안 된다."""
    spans = _line(((14.0, "Deep"),), ((14.0, "Learning"),))
    assert _title_from_spans(spans) == "Deep Learning"


def test_title_skips_arxiv_header_even_when_largest():
    """arXiv 세로 헤더가 제목보다 큰 폰트인 경우에도 제외."""
    spans = _spans(
        (20.0, "arXiv:1409.2329v5  [cs.NE]  19 Feb 2015"),
        (14.0, "Real Paper Title Here"),
        (10.0, "Some Author"),
    )
    assert _title_from_spans(spans) == "Real Paper Title Here"


def test_title_strips_footnote_markers():
    spans = _spans((14.0, "A Great Paper*"), (10.0, "Author"))
    assert _title_from_spans(spans) == "A Great Paper"


def test_title_empty_for_no_spans():
    assert _title_from_spans([]) == ""


def test_title_ignores_later_pages():
    spans = _spans((14.0, "Page One Title")) + _spans(
        (30.0, "HUGE SECOND PAGE HEADING"), page=1)
    assert _title_from_spans(spans) == "Page One Title"


# ------------------------------------------------------------------- 초록

def test_abstract_between_markers():
    text = ("Some Title\nAbstract\nThis is the abstract body. "
            "1 Introduction\nThis is intro.")
    abstract = _abstract_from_text(text)
    assert abstract == "This is the abstract body"     # 끝 마침표는 strip됨
    assert "intro" not in abstract.lower()


def test_abstract_empty_when_no_marker():
    assert _abstract_from_text("Title\nSome body text without the marker.") == ""


# --------------------------------------------------------------- 깨짐 감지

def test_looks_garbled_detects_spurious_spaces():
    # 'Ho c hreiter', 'o v er' 처럼 단일 글자 토큰이 많으면 깨진 것으로 판단
    garbled = "L O N G S H O R T T E R M M E M O R Y o v er extended time in terv als"
    assert _looks_garbled(garbled) is True


def test_looks_garbled_false_for_clean_text():
    clean = ("We present a simple regularization technique for recurrent neural "
             "networks with long short term memory units evaluated on language modeling")
    assert _looks_garbled(clean) is False


def test_looks_garbled_false_for_short_text():
    assert _looks_garbled("a b c d") is False   # 20토큰 미만은 판단 보류
