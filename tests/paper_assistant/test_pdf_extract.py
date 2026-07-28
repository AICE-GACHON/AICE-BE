"""PDF 추출 헬퍼 테스트 (실제 PDF 불필요 — 순수 로직만)."""
from paper_assistant.pdf.extract import (
    _abstract_from_text, _looks_garbled, _title_from_spans)


def _spans(*items):
    """(size, text) → (size, text, page=0) 형태로."""
    return [(sz, t, 0) for sz, t in items]


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
    spans = _spans(
        (20.0, "arXiv:1409.2329v5  [cs.NE]  19 Feb 2015"),   # 헤더 잡동사니
        (10.0, "Under review as a conference paper at ICLR 2015"),
        (17.2, "R"), (13.8, "ECURRENT"),
        (17.2, "N"), (13.8, "EURAL"),
        (17.2, "N"), (13.8, "ETWORK"),
        (17.2, "R"), (13.8, "EGULARIZATION"),
        (10.0, "Wojciech Zaremba"),
    )
    title = _title_from_spans(spans)
    assert title == "RECURRENT NEURAL NETWORK REGULARIZATION"
    assert "arXiv" not in title
    assert "Zaremba" not in title


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
    spans = [(14.0, "Page One Title", 0), (30.0, "HUGE SECOND PAGE HEADING", 1)]
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
