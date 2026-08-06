"""PDF draft → 제목/초록 추출.

**제목은 폰트 크기로 뽑는다.** 논문 첫 페이지에서 제목은 항상 본문·저자보다 큰
폰트를 쓴다(실측: 제목 14.3~17.2pt vs 저자 10pt). 텍스트 순서만 보면 제목 뒤에
바로 붙는 저자 줄을 걸러낼 수 없어서, PyMuPDF의 span 폰트 크기를 신호로 쓴다.

초록은 "Abstract"~"Introduction" 사이를 텍스트에서 잘라낸다.

llm이 주어지면 Haiku로 정제한다(선택). 조판이 깨진 아주 오래된 PDF(1990년대
TeX Type1 등)는 폰트 내부 코드로 합자를 저장해 어떤 추출기로도 복원이 어렵다 —
그 경우 제목/초록 텍스트를 직접 붙여넣는 경로를 안내한다.
"""
import logging
import re

log = logging.getLogger(__name__)

_ABSTRACT = re.compile(r"\babstract\b", re.IGNORECASE)
_INTRO = re.compile(r"\b(?:1\s*\.?\s*)?introduction\b", re.IGNORECASE)

# 제목이 아닌 헤더 잡동사니 (arXiv 줄, 심사 문구, 날짜, 저널 헤더 등)
_HEADER_CRUFT = re.compile(
    r"arxiv:|under review|published as|preprint|proceedings|"
    r"conference paper|workshop|copyright|journal of|vol\.|"
    r"^\W*$|@|https?://|\bdoi\b",
    re.IGNORECASE)

MIN_TITLE_CHARS = 8
MAX_TITLE_CHARS = 250


def page_count(pdf_bytes: bytes) -> int:
    """PDF 페이지 수. 손상된 PDF면 예외가 그대로 올라간다.

    제목·초록 추출과 분리해 둔 이유는 **호출 순서** 때문이다. 페이지 수 상한을 넘는
    문서는 추출을 시도하기 전에 거부해야 한다 — 200페이지짜리를 파싱한 뒤에 거부하면
    그 비용이 그냥 버려진다. 이쪽은 페이지를 읽지 않고 목차만 보므로 훨씬 싸다.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return doc.page_count
    finally:
        doc.close()


def _page_spans(pdf_bytes: bytes, pages: int = 2):
    """(size, text, page_index) 리스트 + 전체 텍스트 반환."""
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    spans, texts = [], []
    for i in range(min(pages, doc.page_count)):
        page = doc[i]
        texts.append(page.get_text("text"))
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:      # 이미지 블록 제외
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    t = span["text"].strip()
                    if t:
                        spans.append((round(span["size"], 1), t, i))
    doc.close()
    return spans, "\n".join(texts)


def _title_from_spans(spans) -> str:
    """첫 페이지에서 가장 큰 폰트 크기의 텍스트를 제목으로 본다.

    제목이 두 줄로 나뉘어도 같은 크기이므로 자연스럽게 이어붙는다.
    저자(10pt)·소속·이메일은 크기가 작아 자동으로 배제된다.
    """
    first_page = [(sz, t) for sz, t, pg in spans if pg == 0]
    if not first_page:
        return ""

    # 헤더 잡동사니를 뺀 뒤 최대 크기를 찾는다 (arXiv 줄이 20pt인 경우가 있음)
    candidates = [(sz, t) for sz, t in first_page if not _HEADER_CRUFT.search(t)]
    if not candidates:
        return ""

    max_size = max(sz for sz, _ in candidates)
    # 최대 크기의 75% 이상인 span을 제목으로 채택.
    # 허용폭을 넓게 잡는 이유: 드롭캡 조판(예: ICLR 스타일)은 첫 글자만 17.2pt,
    # 나머지는 13.8pt여서 좁게 잡으면 "R N N R"처럼 첫 글자만 남는다.
    # 저자(10pt)는 통상 제목의 60% 이하라 이 임계값에서도 배제된다.
    threshold = max_size * 0.75
    parts = [t for sz, t in candidates if sz >= threshold]

    title = " ".join(parts).strip()
    title = re.sub(r"\s+", " ", title)
    # 드롭캡 조판 복원: 큰 첫 글자가 별도 span이라 "R ECURRENT"로 분리된다.
    # 대문자 1개 + 공백 + 대문자 2자 이상 → 붙인다 ("R ECURRENT" → "RECURRENT").
    title = re.sub(r"\b([A-Z]) ([A-Z]{2,})", r"\1\2", title)
    # 각주 기호 정리
    title = re.sub(r"[\*†‡§¶]", "", title).strip()
    return title[:MAX_TITLE_CHARS]


def _abstract_from_text(text: str) -> str:
    """'Abstract'와 'Introduction' 사이를 초록으로 본다."""
    m_abs = _ABSTRACT.search(text)
    if not m_abs:
        return ""
    rest = text[m_abs.end():]
    m_intro = _INTRO.search(rest)
    abstract = rest[:m_intro.start()] if m_intro else rest[:2500]
    return re.sub(r"\s+", " ", abstract).strip(" :.\n")


def _looks_garbled(s: str) -> bool:
    """추출 텍스트가 깨졌는지 대략 판단 (헛공백·합자 오독 등).

    단일 글자 토큰 비율이 높으면 조판이 깨진 것으로 본다(옛날 TeX PDF 등).
    """
    tokens = s.split()
    if len(tokens) < 20:
        return False
    singles = sum(1 for t in tokens if len(t) == 1 and t.isalpha())
    return singles / len(tokens) > 0.18


def extract_title_abstract(pdf_bytes: bytes, llm=None) -> tuple[str, str]:
    """PDF 바이트 → (title, abstract).

    llm이 주어지면 Haiku로 정제한다(저자 잔여물 제거, 줄바꿈 정리 등).
    """
    spans, text = _page_spans(pdf_bytes)
    title = _title_from_spans(spans)
    abstract = _abstract_from_text(text)

    if _looks_garbled(text):
        log.warning("PDF 텍스트 레이어가 손상된 것으로 보입니다 "
                    "(옛날 조판). 제목/초록 직접 입력을 권장합니다.")

    if llm is not None:
        from paper_assistant.graph.llm import HAIKU

        system = (
            "You are given the title and abstract extracted from an academic PDF, "
            "plus the raw first-page text. Clean them up: remove author names, "
            "affiliations, footnote markers, and line-break artifacts from the title; "
            "make sure the abstract is the paper's actual abstract. "
            "Return JSON {\"title\": ..., \"abstract\": ...}. "
            "Keep the abstract faithful to the content; do not summarize or invent.")
        user = (f"EXTRACTED TITLE: {title}\n\n"
                f"EXTRACTED ABSTRACT: {abstract[:2000]}\n\n"
                f"RAW FIRST PAGE:\n{text[:2500]}")
        out = llm.json(HAIKU, system, user, max_tokens=1500)
        if out.get("title"):
            title = out["title"].strip()
        if out.get("abstract"):
            abstract = out["abstract"].strip()

    return title.strip(), abstract.strip()
