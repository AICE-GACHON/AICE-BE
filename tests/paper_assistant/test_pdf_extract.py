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


# =============================================================== 스캔본 비전 폴백
#
# 여기서는 **진짜 PDF를 만들어 쓴다.** 위쪽 테스트들과 달리 검증 대상이 "텍스트
# 레이어가 없다"는 파일 자체의 성질이라, span을 손으로 지어내면 검증이 성립하지
# 않는다. 스캔본은 정상 PDF를 그림으로 구워서 만든다 — 실제 스캔본과 같은 상태다.

import pytest

from paper_assistant.pdf.extract import (
    _VISION_PAGES, _render_pages, extract_title_abstract)

fitz = pytest.importorskip("fitz")

_TITLE = "Deep Residual Learning for Image Recognition"
_ABSTRACT_LINES = [
    "Deeper neural networks are more difficult to train. We present a residual",
    "learning framework to ease the training of networks that are substantially",
    "deeper than those used previously.",
]


def _text_pdf(pages: int = 2) -> bytes:
    """제목·저자·초록·서론이 있는 평범한 논문 PDF (텍스트 레이어 있음)."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), _TITLE, fontsize=16)
    page.insert_text((72, 140), "Kaiming He, Xiangyu Zhang", fontsize=9)
    page.insert_text((72, 180), "Abstract", fontsize=10)
    y = 200
    for line in _ABSTRACT_LINES:
        page.insert_text((72, y), line, fontsize=9)
        y += 14
    page.insert_text((72, y + 20), "1 Introduction", fontsize=10)
    for _ in range(pages - 1):
        doc.new_page().insert_text((72, 100), "body text", fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


def _scanned_pdf(pages: int = 3) -> bytes:
    """텍스트 레이어가 없는 PDF — 페이지를 그림으로 구워 넣는다."""
    src = fitz.open(stream=_text_pdf(pages), filetype="pdf")
    out = fitz.open()
    for i in range(src.page_count):
        pix = src[i].get_pixmap(dpi=100)
        out.new_page(width=pix.width, height=pix.height).insert_image(
            fitz.Rect(0, 0, pix.width, pix.height), pixmap=pix)
    data = out.tobytes()
    src.close()
    out.close()
    return data


class _StubLLM:
    """호출 기록을 남기는 가짜 LLM (API 비용 0)."""

    def __init__(self, vision=None, clean=None, vision_raises=False):
        self._vision = vision or {}
        self._clean = clean or {}
        self._vision_raises = vision_raises
        self.vision_calls: list[list[bytes]] = []
        self.json_calls: list[str] = []

    def json_with_images(self, model, system, images, user, max_tokens=2000):
        self.vision_calls.append(images)
        if self._vision_raises:
            raise RuntimeError("API down")
        return self._vision

    def json(self, model, system, user, max_tokens=1024):
        self.json_calls.append(user)
        return self._clean


def test_scanned_pdf_has_no_text_layer():
    """전제 확인 — 이게 깨지면 아래 테스트들이 아무것도 검증하지 못한다."""
    title, abstract = extract_title_abstract(_scanned_pdf())
    assert title == ""
    assert abstract == ""


def test_vision_recovers_title_and_abstract_from_scan():
    llm = _StubLLM(vision={"title": _TITLE, "abstract": " ".join(_ABSTRACT_LINES)})
    title, abstract = extract_title_abstract(_scanned_pdf(), llm=llm)

    assert title == _TITLE
    assert abstract.startswith("Deeper neural networks")
    assert len(llm.vision_calls) == 1
    assert llm.json_calls == []          # 비전을 탔으면 정제 호출은 낭비다


def test_vision_does_not_run_when_text_layer_is_fine():
    """정상 업로드에 비전 비용이 붙지 않는 것이 이 분기의 전제다."""
    llm = _StubLLM(clean={"title": _TITLE, "abstract": "cleaned abstract"})
    title, abstract = extract_title_abstract(_text_pdf(), llm=llm)

    assert llm.vision_calls == []
    assert len(llm.json_calls) == 1
    assert title == _TITLE
    assert abstract == "cleaned abstract"


def test_vision_failure_falls_back_to_text_extraction():
    """비전이 죽어도 텍스트로 건진 것은 살린다 — 500이 되면 안 된다."""
    llm = _StubLLM(vision_raises=True)
    title, abstract = extract_title_abstract(_scanned_pdf(), llm=llm)

    assert len(llm.vision_calls) == 1
    assert (title, abstract) == ("", "")   # 스캔본이라 텍스트도 비어 있다


def test_vision_result_merges_field_by_field():
    """한쪽만 복원돼도 나머지는 텍스트 추출분을 지킨다.

    'Abstract' 표지가 없어 초록만 비는 PDF다 — 비전은 초록만 돌려주고, 제목은
    텍스트로 뽑은 값이 남아야 한다. 통째로 덮어쓰면 이 경우 제목이 사라진다.
    """
    llm = _StubLLM(vision={"title": "", "abstract": "recovered abstract"})
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), _TITLE, fontsize=16)
    page.insert_text((72, 140), " ".join(_ABSTRACT_LINES), fontsize=9)
    pdf = doc.tobytes()
    doc.close()

    title, abstract = extract_title_abstract(pdf, llm=llm)
    assert len(llm.vision_calls) == 1     # 초록이 비어서 비전이 돌았다
    assert title == _TITLE                # 텍스트로 뽑은 제목이 살아남았다
    assert abstract == "recovered abstract"


def test_render_pages_caps_page_count_and_returns_png():
    pages = _render_pages(_scanned_pdf(pages=5))
    assert len(pages) == _VISION_PAGES
    assert all(p.startswith(b"\x89PNG") for p in pages)


def test_render_pages_stays_under_the_api_downscale_limit():
    """긴 변이 1568px를 넘으면 API가 축소한 뒤 토큰을 매긴다 — 만든 픽셀이 버려진다."""
    png = _render_pages(_text_pdf())[0]
    # PNG IHDR: 바이트 16~24가 width/height (big-endian). 이미지 라이브러리 없이 읽는다.
    w = int.from_bytes(png[16:20], "big")
    h = int.from_bytes(png[20:24], "big")
    assert max(w, h) <= 1568, f"{w}x{h}"


def test_render_pages_handles_single_page_pdf():
    assert len(_render_pages(_text_pdf(pages=1))) == 1
