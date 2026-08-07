"""PDF draft → 제목/초록 추출.

**제목은 폰트 크기로 뽑고, 이어붙이는 것은 좌표로 한다.**

어느 span이 제목인지는 폰트 크기가 알려준다 — 논문 첫 페이지에서 제목은 항상
본문·저자보다 크다(실측: 제목 14.3~17.2pt vs 저자 10pt). 텍스트 순서만 보면 제목 뒤에
바로 붙는 저자 줄을 걸러낼 수 없다.

그 span들을 어떻게 이을지는 **bbox가 알려준다** (_join_spans). 예전에는 전부 공백으로
잇고 정규식으로 복구했는데, 드롭캡 조판이 한 단어를 셋으로 쪼개거나 줄 끝 하이픈이
자기 span으로 떨어지는 경우를 복구하지 못했다 — 실측 실패:
`L ORA: LOW -RANK ADAPTATION OF LARGE LAN GUAGE MODELS`.

⚠️ **소문자 복원은 하지 않는다.** small-caps 조판은 작은 span이 원래 소문자였다는
뜻이라 `LoRA: Low-Rank ...`까지 되살릴 수 있지만, 실측 결과 **검색이 달라지지 않았다**
(대문자판과 원본 casing의 top-10 일치 10/10, 원 논문 순위 동일). 코드와 오작동 위험만
늘어나므로 만들지 않았다.

초록은 "Abstract"~"Introduction" 사이를 텍스트에서 잘라낸다.

llm이 주어지면 Haiku로 정제한다(선택).

**텍스트 레이어가 없거나(스캔본) 깨진 경우** — 1990년대 TeX Type1처럼 폰트 내부
코드로 합자를 저장하는 조판은 어떤 추출기로도 복원되지 않는다 — 앞 2페이지를
그림으로 렌더해 Haiku 비전에 읽힌다(_from_page_images). **실패했을 때만** 돌기
때문에 정상 업로드에는 비용이 붙지 않는다.
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
    """(size, text, page_index, bbox) 리스트 + 전체 텍스트 반환.

    bbox를 함께 들고 오는 이유는 **span 사이에 공백을 넣을지를 좌표로 판단**하기
    위해서다 (_join_spans 참고). 텍스트만으로는 "같은 단어가 쪼개진 것"과 "다른
    단어"를 구별할 수 없다.
    """
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
                    # **strip 하지 않는다.** span 텍스트에 이미 들어 있는 앞뒤 공백이
                    # 진짜 띄어쓰기인 경우가 있고(실측: ' M'), bbox는 그 공백까지
                    # 포함해 잡히므로 좌표만으로는 복원할 수 없다.
                    if span["text"].strip():
                        spans.append(
                            (round(span["size"], 1), span["text"], i, span["bbox"]))
    doc.close()
    return spans, "\n".join(texts)


# span 사이 간격이 이 값(폰트 크기 대비 비율)을 넘으면 진짜 띄어쓰기로 본다.
# 실측(LoRA 논문 ICLR 스타일 제목): 단어 **내부** 간격 0.85pt, 단어 **사이** 10.69pt.
# 폰트 17.2pt 기준으로 0.05 → 0.86, 0.15 → 2.58이라 둘 사이가 넉넉히 벌어진다.
_WORD_GAP_RATIO = 0.15
# 같은 줄로 볼 y 허용 오차(폰트 크기 대비). 드롭캡은 큰 글자와 작은 글자의 윗변이
# 어긋나므로(80.5 vs 83.1) 0으로 두면 매 글자가 새 줄로 잡힌다.
_SAME_LINE_RATIO = 0.6


def _join_spans(parts: list[tuple[str, tuple]], sizes: list[float]) -> str:
    """제목 span들을 좌표를 보고 이어붙인다.

    **모두 공백으로 잇고 정규식으로 복구하는 방식은 못 고치는 경우가 있다.** 실측
    실패 사례(LoRA 논문): `L ORA: LOW -RANK ADAPTATION OF LARGE LAN GUAGE MODELS`.
    드롭캡 조판이 한 단어를 세 조각('L','O','RA:')으로 쪼개면 "대문자 1개 + 공백 +
    대문자 2자 이상" 규칙으로는 복구되지 않고, 줄 끝 하이픈('LAN-' / 'GUAGE')은
    아예 다른 문제다.

    좌표를 쓰면 규칙이 단순해진다:

    - 같은 줄이고 간격이 좁으면 → 붙인다 (쪼개진 한 단어)
    - 같은 줄이고 간격이 넓으면 → 공백 (다른 단어)
    - 줄이 바뀌었는데 앞이 '-'로 끝나면 → **하이픈을 떼고** 붙인다 (줄바꿈 하이픈)
    - 줄이 바뀌었으면 → 공백
    """
    if not parts:
        return ""
    out = [parts[0][0]]
    for i in range(1, len(parts)):
        text, bbox = parts[i]
        _, prev_bbox = parts[i - 1]
        size = sizes[i] or 1.0
        same_line = abs(bbox[1] - prev_bbox[1]) <= size * _SAME_LINE_RATIO
        already_spaced = out[-1].endswith(" ") or text.startswith(" ")
        if same_line:
            gap = bbox[0] - prev_bbox[2]
            wide = gap > size * _WORD_GAP_RATIO
            out.append(" " + text if (wide and not already_spaced) else text)
        elif already_spaced:
            out.append(text)
        elif out[-1].rstrip().endswith("-"):
            # 'LAN-' + 줄바꿈 + 'GUAGE' → 'LANGUAGE'. 하이픈을 남기면 단어가 깨진다.
            out[-1] = out[-1].rstrip()[:-1]
            out.append(text)
        else:
            out.append(" " + text)
    return "".join(out)


def _title_from_spans(spans) -> str:
    """첫 페이지에서 가장 큰 폰트 크기의 텍스트를 제목으로 본다.

    제목이 두 줄로 나뉘어도 같은 크기이므로 자연스럽게 이어붙는다.
    저자(10pt)·소속·이메일은 크기가 작아 자동으로 배제된다.
    """
    first_page = [(sz, t, bbox) for sz, t, pg, bbox in spans if pg == 0]
    if not first_page:
        return ""

    # 헤더 잡동사니를 뺀 뒤 최대 크기를 찾는다 (arXiv 줄이 20pt인 경우가 있음).
    #
    # ⚠️ **글자가 없는 span(구두점만 있는 것)은 잡동사니 검사에서 제외한다.** 예전에는
    # `^\W*$` 규칙이 이런 span을 통째로 버렸는데, 줄바꿈 하이픈이 자기 span으로
    # 떨어져 나오는 조판에서는 그 하이픈이 사라져 단어가 붙지 않았다
    # (실측: 'LAN-' + 'GUAGE'가 'LAN GUAGE'가 됐다).
    candidates = [(sz, t, bbox) for sz, t, bbox in first_page
                  if not (re.search(r"\w", t) and _HEADER_CRUFT.search(t))]
    if not candidates:
        return ""

    max_size = max(sz for sz, _, _ in candidates)
    # 최대 크기의 75% 이상인 span을 제목으로 채택.
    # 허용폭을 넓게 잡는 이유: 드롭캡 조판(예: ICLR 스타일)은 첫 글자만 17.2pt,
    # 나머지는 13.8pt여서 좁게 잡으면 "R N N R"처럼 첫 글자만 남는다.
    # 저자(10pt)는 통상 제목의 60% 이하라 이 임계값에서도 배제된다.
    threshold = max_size * 0.75
    picked = [(sz, t, bbox) for sz, t, bbox in candidates if sz >= threshold]

    # 공백 여부는 좌표로 판단한다 (_join_spans). 정규식 후처리로는 한 단어가 셋으로
    # 쪼개진 경우와 줄바꿈 하이픈을 복구하지 못한다.
    title = _join_spans([(t, bbox) for _, t, bbox in picked],
                        [sz for sz, _, _ in picked])
    title = re.sub(r"\s+", " ", title).strip()
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


# --- 스캔본 대응: 페이지를 그림으로 읽는다 -----------------------------------
#
# 렌더 해상도. Anthropic은 긴 변이 1568px를 넘으면 이미지를 **축소한 뒤** 토큰을
# 계산한다 — 그보다 크게 만들면 우리가 만든 픽셀은 버려지고 인코딩 비용만 든다.
# A4 긴 변 11.69in × 130dpi = 1520px라 상한 바로 아래에 들어간다.
_VISION_DPI = 130
# 제목과 초록은 앞 한두 장에만 있다. 표지가 따로 붙은 스캔본을 고려해 2장.
_VISION_PAGES = 2

_VISION_SYSTEM = (
    "You are looking at page images of an academic paper. Read the title and the "
    "abstract exactly as printed. "
    'Return JSON {"title": ..., "abstract": ...} and nothing else. '
    "Exclude author names, affiliations, footnote markers, and the heading word "
    "'Abstract' itself. Transcribe the abstract verbatim — do not summarize, "
    "shorten, translate, or invent. "
    'If a field is genuinely not visible in these pages, return "" for it.')


def _render_pages(pdf_bytes: bytes, pages: int = _VISION_PAGES,
                  dpi: int = _VISION_DPI) -> list[bytes]:
    """앞쪽 페이지를 PNG로 렌더한다."""
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return [doc[i].get_pixmap(dpi=dpi).tobytes("png")
                for i in range(min(pages, doc.page_count))]
    finally:
        doc.close()


def _from_page_images(pdf_bytes: bytes, llm) -> tuple[str, str]:
    """페이지 그림을 Haiku 비전에 읽혀 (title, abstract)를 복원한다."""
    from paper_assistant.llm import HAIKU

    images = _render_pages(pdf_bytes)
    if not images:
        return "", ""
    out = llm.json_with_images(
        HAIKU, _VISION_SYSTEM, images,
        "Give me the title and the abstract of this paper.")
    return (str(out.get("title") or "").strip(),
            str(out.get("abstract") or "").strip())


def extract_title_abstract(pdf_bytes: bytes, llm=None) -> tuple[str, str]:
    """PDF 바이트 → (title, abstract).

    llm이 주어지면 텍스트 추출 결과에 따라 갈린다:

    - **멀쩡할 때** — Haiku로 정제만 한다 (저자 잔여물 제거, 줄바꿈 정리 등).
    - **비었거나 깨졌을 때** — 페이지를 그림으로 렌더해 Haiku 비전에 읽힌다.
      스캔본과 옛 조판이 여기서 살아난다. 예전에는 이 경우 그냥 422였다.

    비전은 **실패했을 때만** 돈다. 정상 업로드에 비용이 붙지 않는 것이 이 분기의
    전제다. llm이 없으면(LLM off) 예전과 완전히 동일하게 동작한다.

    파이프라인의 나머지는 스캔본을 이미 처리한다 — LLM 재정렬이 PDF를 document
    블록으로 넘기고 API가 페이지를 이미지로 렌더하기 때문이다. 막고 있던 것은
    임베딩에 쓸 제목/초록을 **우리가** 못 뽑는다는 것뿐이었다.
    """
    spans, text = _page_spans(pdf_bytes)
    title = _title_from_spans(spans)
    abstract = _abstract_from_text(text)
    garbled = _looks_garbled(text)

    if llm is None:
        if garbled or not title or not abstract:
            log.warning("PDF 텍스트 추출이 부실합니다 (LLM off라 비전 폴백 없음).")
        return title.strip(), abstract.strip()

    if garbled or not title or not abstract:
        log.info("PDF 텍스트 추출 부실 (title=%s, abstract=%d자, garbled=%s) "
                 "→ 페이지 그림으로 다시 읽습니다.", bool(title), len(abstract), garbled)
        try:
            v_title, v_abstract = _from_page_images(pdf_bytes, llm)
        except Exception:
            # 비전이 실패해도 텍스트로 건진 것은 살린다 — 한쪽만 비어 있을 수 있고,
            # 그 경우 호출자가 부분 결과로 판단할 여지가 남는다.
            log.exception("비전 폴백 실패 — 텍스트 추출 결과를 그대로 씁니다.")
            v_title = v_abstract = ""
        # 필드 단위로 채운다. 비전이 제목만 읽어내는 경우가 있다.
        return (v_title or title).strip(), (v_abstract or abstract).strip()

    # 텍스트가 멀쩡한 정상 경로 — 정제만 한다.
    from paper_assistant.llm import HAIKU

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
