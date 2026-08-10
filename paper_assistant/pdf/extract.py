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
import hashlib
import logging
import re
import statistics

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

# 리비전 본문 diff에서 한 버전의 전체 텍스트를 뽑을 때의 페이지 상한. LLM 비용이
# 걸린 값이 아니라(이 경로엔 LLM이 없다) PyMuPDF 파싱 시간·메모리를 병리적으로 긴
# 첨부(예: 부록을 통째로 붙인 카메라레디)에서 막기 위한 값이다. 제출물 업로드의
# MAX_PAGE_COUNT와 우연히 같지만 근거는 별개다.
MAX_BODY_PAGES = 60

# "under review" 조판이 본문 옆에 세로로 박아넣는 리뷰어 인용용 줄번호(000, 001 ...).
# 블록 추출기가 숫자 하나하나를 별도 블록으로 뽑아내므로 문단으로 세면 안 된다.
_LINE_NUMBER = re.compile(r"\d{1,4}")

# Figure/Table 캡션. 그림·표는 이미지 자체를 못 가져오므로 본문에 자리만
# 남기고("(그림 3)"/"(표 1)"), 캡션 문구는 유용한 정보라 그대로 둔다.
#
# ⚠️ 번호 뒤에 콜론/마침표가 오는 것까지 요구한다. "Figure 2:"(캡션)와 "Figure 2
# illustrates..."(그냥 본문 문장이 우연히 이 단어로 시작)를 구분해야 한다 —
# 실측 실패: 콜론 없이 매칭했더니 본문 문장이 캡션으로 오인돼 그림 크롭이
# 엉뚱한 페이지의 문단을 잘라왔다.
_CAPTION = re.compile(r"^(figure|table)\s+(\d+)[:.]", re.IGNORECASE)

# Algorithm 캡션은 콜론/마침표 요구를 뺐다 — algorithmic/algorithm2e 패키지가
# 흔히 "Algorithm 1 <제목>."처럼 번호 바로 뒤에 구두점 없이 쓴다(실측: 이
# 요구를 그대로 쓰면 캡션 자체가 안 걸러져 의사코드가 통째로 본문에 깨진
# 텍스트로 샌다). 대신 본문 오탐 방지는 텍스트가 아니라 기하학적 확인으로
# 한다 — _table_regions가 이 후보 바로 아래에 괘선이 2개 이상 있을 때만
# 진짜 캡션으로 인정한다("Algorithm 1이 가장 좋다" 같은 본문 서술은 뒤에
# 괘선이 없으므로 자연히 걸러진다).
_ALGO_CAPTION = re.compile(r"^algorithm\s+(\d+)\b", re.IGNORECASE)
_CAPTION_LABEL_WORD = {"figure": "그림", "table": "표", "algorithm": "알고리즘"}

# 문장이 페이지/컬럼 경계에 낀 전체 폭 그림 때문에 둘로 쪼개졌는지 판단하는 기준.
# 앞 문단이 문장부호로 안 끝나고 뒤 문단이 소문자로 시작하면 원래 한 문장이었던
# 것으로 본다(_stitch_split_sentences).
_SENTENCE_END = re.compile(r"[.!?:;][\"')\]]*$")
_STARTS_LOWER = re.compile(r"^[a-z]")
_MEDIA_PLACEHOLDER = re.compile(r"^\((?:(?:그림|표|알고리즘|수식)\s*\d*|박스\s*[0-9a-f]+)\)$")
# _MEDIA_PLACEHOLDER와 달리 끝(`$`)을 고정하지 않는다 — "(표 5 — 이미지
# 추출실패, 텍스트로 대체)\n\n{캡션 본문}"처럼 순수 placeholder가 아니라
# 뒤에 실패 사유·원문 캡션이 이어지는 경우도 "이 문단이 그림/표 자리에서
# 시작한다"는 사실만으로 매칭해야 한다(_stitch_split_sentences 참고).
_MEDIA_PLACEHOLDER_OPENING = re.compile(r"^\((?:그림|표|알고리즘|수식|박스)\s*[^)]*\)")

# 독립 수식 번호 — "(1)", "(23)" 같은 LaTeX 기본 수식 번호 매기기. Figure/Table과
# 달리 "Equation N:" 같은 이름표가 없어 텍스트만으로는 캡션인지 본문 속 괄호
# 표기(예: "V(0)=0")인지 구분이 안 된다 — _equation_regions가 좌표로 구분한다.
_EQUATION_LABEL = re.compile(r"^\((\d{1,3})\)$")
_EQ_RIGHT_MARGIN_TOLERANCE = 3.0
_EQ_LEFT_MARGIN_TOLERANCE = 3.0

# extract_full_text가 "이 블록은 그림/표/수식 영역 안에 완전히 포함되는가"를
# 판단할 때 쓰는 여유값(pt). 영역 bbox는 두 가지 서로 다른 세분화 단위로
# 계산된다 — _ordered_blocks의 병합 블록(그림/표 캡션 판단용)과
# _equation_regions의 줄(line) 단위 스캔(수식 이미지 크롭용) — 두 값이
# 같은 내용을 가리켜도 소수점 몇 pt 차이가 날 수 있다(실측: 표 헤더 바로
# 아래 첫 데이터 행이 0.4pt, 수식 본문 블록이 2~3pt 차이로 완전 포함
# 조건에서 아슬아슬하게 빠짐 — 진짜 별개 문단이었다면 보통 10pt 이상
# 벌어지므로 이 정도 여유는 오탐 위험이 없다).
_REGION_CONTAINMENT_TOLERANCE = 8.0

# 한 "줄"(get_text("dict")의 line)에 이 단어 수를 넘으면 자연어 문단 줄로,
# 이하면 수식 조각 줄로 본다. 실측: 이 논문 컬럼 폭 기준 진짜 문단 줄은
# 12~17단어인데 수식 조각 줄("zt =", "p", "1 −γ(t)ϵ," 등)은 1~2단어뿐이라
# 격차가 크다 — 6은 그 사이 어디에 둬도 안전한 값이다.
_EQ_PROSE_LINE_MIN_WORDS = 6

# 라벨 줄 다음으로 "수식이 계속되는 줄"로 인정해 확장할 최대 줄 수. 단어 수
# 적음만으로 판단하면 소제목("5 EXPERIMENTS")이나 짧은 문장("This concludes
# our analysis.")도 수식으로 오인해 삼킬 수 있다 — 상한을 둬서 오탐이 나도
# 몇 줄 정도의 피해로 막는다. 실측한 다중 행 수식(제약식 "s.t. ..." 한두 줄
# 추가)은 보통 1~2줄이라 3이면 정상 케이스는 다 덮으면서 과확장은 막는다.
_EQ_CONTINUATION_MAX_LINES = 3


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


# get_text("text", clip=rect)로 재추출한 텍스트 끝에 이만큼(개수) 이상
# 연속으로 1~3자짜리 순수 알파벳 토큰이 붙어 있으면 "clip 경계에 걸쳐
# 글자가 토막난 것"으로 의심한다(_strip_clip_boundary_leak).
_MIN_LEAK_RUN = 5
_SHORT_ALPHA_TOKEN = re.compile(r"^[A-Za-z]{1,3}$")


def _strip_clip_boundary_leak(page, rect, text: str) -> str:
    """clip 재추출 결과 끝에 토막난 글자가 새어 붙었으면 잘라낸다.

    page.get_text("text", clip=rect)는 rect 경계에 걸친 줄을 부분적으로만
    포함시켜 글자를 뒤섞을 수 있다(실측: 병합된 그룹의 bbox union이 —
    병합 기준(0.8 겹침)에는 못 미쳐 남은, 바로 다음 문단 블록의 시작
    부분과 세로로 살짝 겹치면, 그 문단의 뒷부분 글자가 "the following
    paper"→"h f ll i" 식으로 토막나 끝에 끼어든다). 이 현상은 항상 끝에
    아주 짧은(1~3자) 알파벳 토큰이 여럿 연달아 붙는 형태로 나타난다 —
    실제 수식(첨자 변수 W, U, V 등)도 짧은 토큰이 섞이지만, 진짜
    문장이라면 어딘가에 마침표·긴 단어가 있어 이렇게 끝까지 짧은 토큰만
    이어지지 않는다.

    다만 이 패턴만으로 바로 잘라내면 위험하다 — "for U, V, and W"처럼
    실제로 짧은 변수명 나열로 문장이 끝나는 정상 케이스를 오탐할 수
    있다. 그래서 clip과 무관하게 동작하는 page.get_textbox(rect)로 같은
    구간을 한 번 더 뽑아 **검증만** 한다 — 의심되는 꼬리 문자열이
    거기에도 그대로 있으면(=진짜 내용) 자르지 않는다. get_textbox의
    결과를 그대로 쓰지는 않는다 — 이 함수도 자기 나름의 결함이 있다
    (실측: 시작 부분에 엉뚱한 글자가 붙거나, 특정 폰트의 합 기호(Σ)가
    대체 문자로 깨짐) — 오직 "이 꼬리가 실존하는 내용인가"만 대조하는
    용도로 쓴다.
    """
    tokens = re.split(r"(\s+)", text)
    word_tokens = [t for t in tokens if t.strip()]
    run: list[str] = []
    for t in reversed(word_tokens):
        if _SHORT_ALPHA_TOKEN.match(t):
            run.append(t)
        else:
            break
    if len(run) < _MIN_LEAK_RUN:
        return text
    run.reverse()
    run_str = " ".join(run)
    reference = " ".join(page.get_textbox(rect).split())
    if run_str in reference:
        return text
    word_positions = [i for i, t in enumerate(tokens) if t.strip()]
    cut_at = word_positions[-len(run)]
    return "".join(tokens[:cut_at]).rstrip()


def _ordered_blocks(page) -> list:
    """page.get_text("blocks")와 같은 모양(x0,y0,x1,y1,text,block_no,type)을
    돌려주되, 텍스트 내용은 훨씬 정확하다 — 호출부(_figure_regions,
    extract_full_text 등)는 코드를 바꿀 필요 없이 이 함수로 교체만 하면 된다.

    ⚠️ **"blocks" 모드의 텍스트(b[4])는 수식이 많으면 글자 순서가 실제로
    깨진다.** 수식은 분수·첨자처럼 세로로도 쌓이는데, "blocks" 모드는 자체
    줄 나누기 로직으로 이걸 하나의 선형 순서로 펴면서 순서를 잘못 잡는다
    (실측: "The task-fused features are fed into the level-interaction
    phase."라는 완전한 문장이 수식 정의 한가운데 끼어들어가 나옴). 반면
    page.get_text("text")는 PDF 콘텐츠 스트림에 원래 기록된 순서(LaTeX가
    실제로 그린 순서, 즉 논리적 순서와 대체로 일치)를 그대로 보존해서 같은
    구간에서 문제가 없다(직접 대조 확인함).

    다만 "text" 모드는 좌표 하나로 전체를 뽑아버리므로, 그림/표/박스 영역
    판단에 필요한 "블록 하나하나의 위치"라는 정보가 없다. 그래서 "blocks"
    모드로 일단 블록 경계(bbox)만 얻은 뒤, **그 bbox 범위로 다시
    get_text("text", clip=bbox)를 호출**해 내용만 바꿔치기한다 — 경계
    판단 로직(figure/table/box 영역, 문단 병합 간격 등)은 전부 bbox
    좌표만 쓰므로 전혀 안 건드린다.

    ⚠️ **수식 구간에서는 "blocks" 모드 자체가 서로 겹치는 블록을 여러 개
    만든다.** 첨자 때문에 y좌표가 들쭉날쭉해지면서, 원래 한 덩어리여야 할
    영역이 여러 블록으로 쪼개지고 그 블록들의 bbox가 서로 겹친다(실측: 블록
    A(108~504, 492~517)가 블록 B(120~247, 493~507)를 통째로 포함). 이대로
    각 블록 bbox마다 따로 clip 재추출을 하면 겹치는 부분 내용이 중복으로
    나온다 — 그래서 겹치는 블록들을 먼저 하나의 bbox로 합친 뒤에 재추출한다.
    일반 문단은 애초에 블록끼리 안 겹치므로(각 문단이 독립된 블록) 이 병합은
    수식처럼 실제로 겹치는 경우에만 작동하고, 정상 블록 구조에는 영향이 없다.

    ⚠️ **아주 살짝만 겹쳐도 병합하면 좌우 배치 레이아웃에서 서로 다른
    문단이 하나로 뭉쳐진다.** 실측: 그림이 왼쪽 칸에 있고 그 캡션이 왼쪽
    칸 끝까지 이어지는 동안, 오른쪽 칸은 이미 끝나서 다음 문단이 페이지
    전체 폭으로 재개되면 그 전체 폭 문단의 시작 y좌표가 캡션의 끝나기
    직전 y좌표와 겹친다(칸 높이가 서로 달라서 생기는 자연스러운 현상) —
    이때 완전 겹침이 아니라 한쪽 모서리만 살짝 겹치므로, 겹친 넓이가 더
    작은 블록 면적의 대부분(임계값 이상)을 차지할 때만 "진짜로 같은
    덩어리"로 보고 합친다. 수식 사례처럼 한쪽이 다른 쪽을 통째로 포함하면
    이 비율이 100%에 가까워 여전히 병합되고, 좌우 배치처럼 모서리만 살짝
    겹치는 경우는 비율이 낮아 병합되지 않는다.

    ⚠️ **비율 기준만으로는 부족하다 — 캡션이 옆 칸 전체 폭 문단에 통째로
    "포함"되는 경우가 있다.** 실측: 그림이 왼쪽 절반 아래쪽에 있고 캡션도
    좁게 그 밑에 있는데, 그 옆(오른쪽 칸)이 이미 끝나 있어서 다음 절
    문단이 캡션과 겹치는 세로 구간까지 페이지 전체 폭으로 재개되면, 좁은
    캡션의 bbox가 그 전체 폭 문단의 bbox 안에 100% 포함된다 — 비율로는
    수식 사례와 구분이 안 된다. 캡션으로 시작하는 블록("Figure N:" 등)은
    애초에 완전한 문장이므로 다른 블록과 병합될 이유가 없다 — 아예 병합
    후보에서 제외한다.
    """
    import fitz  # PyMuPDF

    # 겹친 넓이가 더 작은 블록 면적의 이 비율 이상일 때만 "같은 논리적
    # 블록이 쪼개진 것"으로 보고 합친다 — 완전/거의 포함(수식 사례)과
    # 모서리만 살짝 겹치는 우연한 인접(칸 높이 차이로 인한 레이아웃 재개)을
    # 구분하는 기준.
    _CONTAINMENT_RATIO = 0.8

    def _mostly_contains(a: "fitz.Rect", b: "fitz.Rect") -> bool:
        if not a.intersects(b):
            return False
        inter_area = (a & b).get_area()
        smaller_area = min(a.get_area(), b.get_area())
        return smaller_area > 0 and inter_area / smaller_area >= _CONTAINMENT_RATIO

    raw_blocks = page.get_text("blocks")
    image_blocks = [b for b in raw_blocks if b[6] != 0]
    text_blocks = [b for b in raw_blocks if b[6] == 0]

    rects = [fitz.Rect(b[0], b[1], b[2], b[3]) for b in text_blocks]
    is_caption_like = [bool(_CAPTION.match(b[4].strip())) for b in text_blocks]
    sources: list[list[int]] = [[i] for i in range(len(rects))]
    merged: list[fitz.Rect] = []
    changed = True
    while changed:
        changed = False
        next_round: list[fitz.Rect] = []
        next_flags: list[bool] = []
        next_sources: list[list[int]] = []
        used = [False] * len(rects)
        for i, r in enumerate(rects):
            if used[i]:
                continue
            cur = r
            cur_is_caption = is_caption_like[i]
            cur_sources = list(sources[i])
            used[i] = True
            for j in range(i + 1, len(rects)):
                if (not used[j] and not cur_is_caption and not is_caption_like[j]
                        and _mostly_contains(cur, rects[j])):
                    cur |= rects[j]
                    cur_sources.extend(sources[j])
                    used[j] = True
                    changed = True
            next_round.append(cur)
            next_flags.append(cur_is_caption)
            next_sources.append(cur_sources)
        rects = next_round
        is_caption_like = next_flags
        sources = next_sources
    merged = rects

    out = []
    for i, r in enumerate(merged):
        # 여러 원본 블록을 실제로 합친 경우에만 clip 재추출로 순서를
        # 바로잡는다. 합치지 않은 단일 블록은 원본 텍스트를 그대로 쓴다 —
        # 캡션처럼 병합을 거부한 좁은 블록의 bbox가 옆의 넓은(안 합쳐진)
        # 블록 bbox 안에 기하학적으로 겹쳐 있으면, 그 넓은 블록을 clip으로
        # 다시 뽑을 때 캡션 텍스트까지 같이 딸려 나와 중복·오염된다(실측:
        # 본문 문단 재추출 결과 맨 앞에 옆 칸 캡션 문장이 끼어들어 옴).
        if len(sources[i]) == 1:
            text = text_blocks[sources[i][0]][4]
        else:
            text = _strip_clip_boundary_leak(page, r, page.get_text("text", clip=r))
        if text.strip():
            out.append((r.x0, r.y0, r.x1, r.y1, text, i, 0))
    out.extend(image_blocks)
    out.sort(key=lambda b: b[1])
    return out


# 그림 영역의 세로 범위(last_prose_y1~caption) 안에서 찾은 실제 벡터 드로잉/
# 이미지 중, 이 비율보다 작은 도형(가로나 세로가 짧은 선·강조 밑줄 등)은
# "그림 내용"으로 안 본다. 표 괘선 판단(_MIN_RULE_WIDTH_RATIO)과 같은
# 원칙 — 순수 장식성 선이 그림의 가로 범위를 왜곡하지 않게 한다.
_MIN_FIGURE_SHAPE_SIZE = 2.0


def _figure_content_x_range(page, top: float, bottom: float, page_width: float) -> tuple[float, float]:
    """이 세로 범위 안의 실제 그림 내용(벡터 드로잉·이미지)이 차지하는 가로
    범위. 아무 것도 못 찾으면(드문 경우) 페이지 전체 폭으로 방어적으로
    되돌린다 — 지금까지의 동작(전체 폭 크롭)과 같아 회귀 위험이 없다.

    그림이 "Figure N" 캡션 바로 앞의 마지막 문단부터 캡션까지"라는 텍스트
    흐름만으로 판단되면, 그림이 페이지 폭 일부만 차지하고 옆에 본문 텍스트가
    계속 이어지는 레이아웃(예: TikZ 다이어그램이 왼쪽 절반, 오른쪽 절반은
    본문)에서 그 텍스트까지 그림 크롭 안에 같이 잘려 들어가거나, 반대로
    본문에서 그 텍스트가 "그림 영역 안"으로 오인돼 통째로 사라진다. 실제
    도형·이미지의 좌표를 봐야 이 폭을 정확히 알 수 있다.
    """
    x0s: list[float] = []
    x1s: list[float] = []
    for d in page.get_drawings():
        rect = d.get("rect")
        if rect is None or rect.width < _MIN_FIGURE_SHAPE_SIZE or rect.height < _MIN_FIGURE_SHAPE_SIZE:
            continue
        if rect.y1 <= top or rect.y0 >= bottom:
            continue
        x0s.append(rect.x0)
        x1s.append(rect.x1)
    for img in page.get_image_info():
        ix0, iy0, ix1, iy1 = img["bbox"]
        if iy1 <= top or iy0 >= bottom:
            continue
        x0s.append(ix0)
        x1s.append(ix1)
    if not x0s:
        return 0.0, page_width
    return min(x0s), max(x1s)


def _figure_image_top(page, floor: float, ceiling: float) -> float | None:
    """floor~ceiling 범위 안에서 실제 그림 내용(벡터 드로잉·이미지)의 가장
    위쪽 y좌표. 못 찾으면 None.

    텍스트 흐름으로 구한 "마지막 진짜 문단" 경계는 옆 칸에서 계속되는 별개
    본문이나 우연히 근처에 낀 다른 문단 때문에 실제 이미지보다 너무
    늦거나(실측: 그림이 왼쪽 절반 위쪽에, 오른쪽 절반은 그 아래까지 이어지는
    본문이 있어 상단이 캡션 근처까지 밀림) 너무 이를(실측: 이미지 앞에
    상관없는 문단과 섹션 제목까지 함께 포함) 수 있다. 캡션 바로 위 구간에서
    실제 이미지/드로잉을 찾으면 그 좌표가 텍스트 흐름 추정보다 항상 더
    믿을 만하므로, 찾아지면 방향에 관계없이 그쪽을 그림 상단으로 쓴다.
    이미지를 못 찾은 극히 드문 경우(순수 텍스트 설명형 등)에만 텍스트 흐름
    기준선으로 되돌아간다.
    """
    tops: list[float] = []
    for d in page.get_drawings():
        rect = d.get("rect")
        if rect is None or rect.width < _MIN_FIGURE_SHAPE_SIZE or rect.height < _MIN_FIGURE_SHAPE_SIZE:
            continue
        if rect.y1 <= floor or rect.y0 >= ceiling:
            continue
        tops.append(rect.y0)
    for img in page.get_image_info():
        iy0, iy1 = img["bbox"][1], img["bbox"][3]
        if iy1 <= floor or iy0 >= ceiling:
            continue
        tops.append(iy0)
    return min(tops) if tops else None


def _x_overlap(a_x0: float, a_x1: float, b_x0: float, b_x1: float) -> float:
    return min(a_x1, b_x1) - max(a_x0, b_x0)


def _figure_regions(page, blocks: list,
                    table_bottoms: tuple[float, ...] = ()) -> list[tuple[float, float, float, float, str]]:
    """페이지에서 그림 영역을 (region_top, region_bottom, x0, x1, label)로 찾는다.

    "Figure N" 캡션 바로 앞의 마지막 '진짜 본문 문단'(20단어 초과) 끝부터
    캡션 끝까지를 그 그림에 속한 세로 범위로 본다 — extract_full_text(라벨
    텍스트 제거)와 extract_figures(크롭)가 "이 영역은 그림에 속한다"는
    판단을 여기 하나로 공유한다. 따로 판단하면 한쪽만 고쳤을 때 본문에서는
    지워졌는데 크롭 범위는 다르게 계산되는 식으로 어긋날 수 있다.

    가로 범위(x0/x1)는 텍스트 흐름이 아니라 _figure_content_x_range로 그
    세로 범위 안의 실제 그림 내용(벡터 드로잉·이미지)을 찾아서 정한다 —
    "캡션 앞뒤 빈 공간"이라는 가정은 그림이 페이지 전체 폭일 때만 맞고,
    그림이 폭 일부만 차지하며 옆에 본문이 계속되는 레이아웃에서는 틀린다.

    table_bottoms: 이 페이지에서 찾은 표들의 하단 y좌표(_table_regions 결과).
    표 캡션이나 표 데이터 행이 우연히 20단어를 넘기면 "진짜 문단"으로 오인돼
    다음 그림의 시작 지점이 표 중간으로 당겨질 수 있다(실측: 캡션이 긴 표
    바로 다음에 그림이 오는 레이아웃에서, 표 데이터 행이 마침 20단어를 넘겨
    우연히 정상 동작했다 — 짧은 행이었다면 표 전체가 다음 그림 크롭에
    같이 잘려 들어갔을 것). 표를 지났으면 그 표 하단보다 앞으로는 그림
    영역이 시작되지 않도록 최소 기준선을 강제한다.

    ⚠️ **그림이 프로즈 문단 없이 바로 연달아 나오면 앞 그림째 삼킨다.**
    "마지막 진짜 문단" 기준선은 프로즈·표만 갱신하고 그림 자신은 갱신하지
    않았다 — 실측: 부록 페이지에 그림 두 개가 프로즈 없이 캡션만 두고
    붙어 있으면, 두 번째 그림의 "마지막 진짜 문단"이 그 페이지에 아예 없어
    첫 번째 그림까지 포함한 페이지 맨 위부터를 통째로 자기 영역으로 삼는다
    (실측: "Figure 7" 크롭 안에 앞선 "Figure 6" 전체가 통째로 들어감). 그림
    캡션을 지날 때마다도 기준선을 그 캡션 하단으로 올려서, 다음 그림이 이번
    그림 영역을 다시 삼키지 않게 한다.
    """
    page_width = page.rect.width
    sorted_blocks = sorted(blocks, key=lambda bl: bl[1])
    regions = []
    last_prose_y1 = 0.0
    # 그림 캡션과 세로로 겹치는(=같은 높이에서 옆 칸에 흐르는) 본문 블록은
    # "그림 바로 앞 문단"이 될 수 없다 — Y좌표만으로 정렬하면 옆 칸 문단이
    # 캡션보다 근소하게 먼저 끝났다는 이유만으로 기준선을 캡션 하단 너머로
    # 밀어버려 top > bottom인 퇴화 영역이 나온다(실측: 캡션이 왼쪽 절반,
    # 본문이 오른쪽 절반을 계속 채우는 레이아웃). 캡션과 겹치는 블록은
    # 기여분을 그 캡션이 시작되는 지점(y0)까지로 제한해, "캡션이 시작되기
    # 전까지는 분명히 진행 중이던 본문"만큼만 인정한다.
    caption_spans = []
    for b in sorted_blocks:
        if b[6] != 0:
            continue
        text = _flatten_block_text(b[4])
        m = _CAPTION.match(text)
        if m and m.group(1).lower() == "figure":
            caption_spans.append((b[1], b[3]))

    prev_region_bottom = 0.0
    for b in sorted_blocks:
        for tb in table_bottoms:
            if tb <= b[1]:
                last_prose_y1 = max(last_prose_y1, tb)
                prev_region_bottom = max(prev_region_bottom, tb)
        if b[6] != 0:
            continue
        text = _flatten_block_text(b[4])
        m = _CAPTION.match(text)
        if m and m.group(1).lower() == "figure":
            top = last_prose_y1
            image_top = _figure_image_top(page, prev_region_bottom, b[1])
            if image_top is not None:
                top = image_top
            x0, x1 = _figure_content_x_range(page, top, b[3], page_width)
            # ⚠️ **이미지 위·왼쪽·오른쪽에 그 이미지에 속한 라벨(칼럼 제목,
            # 세로로 돌린 행 라벨 등)이 있으면 이미지 좌표만으로는 못
            # 잡는다**(실측 1: "Input images"/"Application of predicted
            # materials"/"Post-editing"이라는 세 칼럼 제목이 각자 자기 아래
            # 이미지 칼럼 바로 위, 좁은 간격을 두고 있어 top이 그 아래로
            # 잡힘. 실측 2: "Blender (Row 1-3)" 같은 세로로 돌린 행 라벨이
            # 이미지 왼쪽에, 역시 좁은 간격(2.7pt)을 두고 있어 x0가 그
            # 오른쪽으로 잡힘). 28599에서 고친 "옆 칸 무관한 문단" 사례와는
            # 반대 방향 — 거긴 그림과 다른 축이었고, 이건 그림과 같은
            # 축(칼럼 제목은 이미지와 같은 가로 범위, 행 라벨은 이미지와
            # 같은 세로 범위)에 좁은 간격으로 붙어 있다는 게 핵심 차이다.
            # 네 방향(위/왼쪽/오른쪽) 다 같은 원리로 확인한다 — 그림의
            # 나머지 한 축과 겹치면서 좁은 간격에 있는 텍스트는 그림
            # 자신의 라벨로 보고 그쪽 경계를 끌어당긴다.
            _line_heights = _line_height_samples(blocks)
            _typical_line_height = statistics.median(_line_heights) if _line_heights else 12.0
            _label_gap = _typical_line_height * 1.5

            def _is_real_label(hb) -> bool:
                # ⚠️ **짧은 라벨과 진짜 옆 칸 문단을 단어 수로 가른다**(실측:
                # 이 검사에 단어 수 제한이 없었을 때, Figure 2 오른쪽의
                # "On the effect of Rank. In Figure 2, we show how..."라는
                # 완전히 무관한 옆 칸 문단이 겨우 2pt 간격이라는 이유만으로
                # "그림에 붙은 라벨"로 오인돼 통째로 그림 크롭 안에 들어갔다
                # — 이건 28599에서 이미 고친 "옆 칸 무관한 문단" 문제가
                # 그대로 재발한 것이었다). "Input images", "Application of
                # predicted materials", "Blender (Row 1-3)"처럼 진짜 라벨은
                # 전부 10단어 미만인 반면, 문단은 한 문장만 돼도 훨씬 길다
                # — 이 격차로 확실하게 구분한다.
                if hb[6] != 0:
                    return False
                hb_tokens = _flatten_block_text(hb[4]).split()
                if not hb_tokens or all(_LINE_NUMBER.fullmatch(t) for t in hb_tokens):
                    return False
                return len(hb_tokens) <= 10

            header_candidates = [
                hb for hb in sorted_blocks
                if _is_real_label(hb) and hb[3] <= top and top - hb[3] <= _label_gap
                and hb[1] >= prev_region_bottom
                and _x_overlap(hb[0], hb[2], x0, x1) > 0
            ]
            if header_candidates:
                top = min(hb[1] for hb in header_candidates)
            left_candidates = [
                hb for hb in sorted_blocks
                if _is_real_label(hb) and hb[2] <= x0 and x0 - hb[2] <= _label_gap
                and _x_overlap(hb[1], hb[3], top, b[3]) > 0
            ]
            if left_candidates:
                x0 = min(hb[0] for hb in left_candidates)
            right_candidates = [
                hb for hb in sorted_blocks
                if _is_real_label(hb) and hb[0] >= x1 and hb[0] - x1 <= _label_gap
                and _x_overlap(hb[1], hb[3], top, b[3]) > 0
            ]
            if right_candidates:
                x1 = max(hb[2] for hb in right_candidates)
            # 캡션 자신의 가로 범위도 항상 포함한다 — 이미지는 칸 폭만큼
            # 좁아도(예: 왼쪽 절반) 캡션 텍스트는 그 아래에서 옆 칸이 이미
            # 끝나 있으면 한 칸 전체 폭(예: 108~504)까지 자유롭게 줄바꿈한다
            # (실측). 이미지 폭만 쓰면 캡션의 오른쪽 절반이 크롭 밖에서
            # 잘려 나온다.
            x0, x1 = min(x0, b[0]), max(x1, b[2])
            regions.append((top, b[3], x0, x1, f"Figure {m.group(2)}"))
            last_prose_y1 = b[3]
            prev_region_bottom = b[3]
            continue
        tokens = text.split()
        is_line_numbers = tokens and all(_LINE_NUMBER.fullmatch(t) for t in tokens)
        if len(tokens) > 20 and not is_line_numbers:
            contribution = b[3]
            for cy0, cy1 in caption_spans:
                if _x_overlap(b[1], b[3], cy0, cy1) > 0:
                    contribution = min(contribution, cy0)
            last_prose_y1 = max(last_prose_y1, contribution)
    return regions


# 이 비율 이상의 폭을 가진 블록만 "일반 문단"으로 보고 페이지의 우측 기준선을
# 재는 데 쓴다 — 페이지 번호·짧은 라벨처럼 폭이 좁은 블록이 섞여 기준선을
# 왜곡하지 않게 한다. 비율로 두어(고정 pt 아님) 페이지 크기가 다른 문서에도
# 그대로 적용된다.
_MIN_PROSE_WIDTH_RATIO = 0.3

# 영역(그림/표/박스/수식)의 가로 폭이 페이지 폭의 이 비율 이상이면 "사실상
# 페이지 전체 폭"으로 보고 기존처럼 여백만 남기고 크롭한다. 이보다 좁으면
# 페이지 폭 일부만 차지하는 레이아웃(그림 옆에 본문 텍스트가 이어지는 경우
# 등)으로 보고 실제 내용 폭만큼만 크롭한다 — 두 경우를 가르는 기준도 고정
# pt 대신 비율로 둬서 페이지 크기가 다른 문서에 그대로 적용된다.
_FULL_WIDTH_REGION_RATIO = 0.85

# extract_media_images가 그림/표/박스/수식 어떤 영역이든 이보다 낮으면 "크롭할
# 가치가 없는 크기"로 보고 이미지를 만들지 않는다.
_MIN_CROP_HEIGHT = 20.0


def _page_text_right_edge(blocks: list, page_width: float) -> float | None:
    """이 페이지 일반 문단들이 도달하는 오른쪽 끝 좌표의 최댓값.

    독립 수식 번호("(1)" 등)가 진짜 캡션인지, 아니면 수식 안의 괄호 표기(예:
    "V(0)=0")인지 텍스트만으로는 구분이 안 된다 — 좌표로 구분한다. LaTeX
    기본 수식 번호 매기기는 항상 이 "본문이 끝나는 오른쪽 기준선"에 정확히
    맞춰 찍힌다(실측: 서로 다른 두 논문에서 모두 소수점까지 동일한 x좌표).
    페이지 자기 자신의 문단 폭으로 기준선을 재므로 여백 설정이 다른 논문에도
    그대로 일반화된다 — 논문마다 하드코딩된 여백값을 쓰지 않는다.
    """
    widths = [b[2] for b in blocks if b[6] == 0 and (b[2] - b[0]) > page_width * _MIN_PROSE_WIDTH_RATIO]
    return max(widths) if widths else None


def _page_text_left_edge(blocks: list, page_width: float) -> float | None:
    """이 페이지 일반 문단들이 시작하는 왼쪽 끝 좌표의 최솟값.

    _page_text_right_edge와 대칭인 값. 수식은 가운데 정렬·들여쓰기 때문에
    좌우 여백이 본문 문단보다 뚜렷이 크다(실측: 이 페이지 일반 문단은 전부
    x0=108.0에서 시작하는데, 같은 페이지 수식 조각들은 x0=150~230대) —
    _equation_regions가 "다음 줄이 진짜 문단인지"를 이 기준선에 딱 붙어
    시작하는지로도 판단한다.
    """
    starts = [b[0] for b in blocks if b[6] == 0 and (b[2] - b[0]) > page_width * _MIN_PROSE_WIDTH_RATIO]
    return min(starts) if starts else None


def _equation_regions(page) -> list[tuple[float, float, float, float, str]]:
    """독립 수식 영역을 (region_top, region_bottom, x0, x1, "Equation N")으로
    찾는다. 수식은 그림·표와 달리 옆에 나란히 텍스트가 오는 레이아웃이
    실측된 적이 없어 x0/x1은 항상 페이지 전체 폭으로 둔다(다른 세 영역
    함수와 반환 형태만 맞춘다).

    Figure/Table과 달리 "Equation N:" 같은 이름표가 본문에 없다 — 있는 건
    수식 끝에 오른쪽 정렬로 찍히는 순번 "(N)"뿐이다. 이 순번만으로는 본문
    괄호 표기(예: 리아푸노프 함수의 "V(0)=0")와 구분이 안 되므로, **페이지의
    오른쪽 기준선(_page_text_right_edge)에 실제로 붙어 있는지**로 걸러낸다
    (실측: 진짜 수식 번호는 오차 0으로 그 기준선에 맞고, 수식 안의 괄호
    표기는 기준선보다 100pt 이상 안쪽에 있다).

    캡션이 없으므로 지금은 번호가 같을 때만 "같은 수식"으로 본다(재배치 시
    캡션 문구로 재매칭하는 Figure/Table의 정교한 로직은 아직 없다 — 수식은
    설명 문구가 없어 그 방식 자체를 못 쓴다). 번호가 바뀌면 삭제+삽입으로
    보이는 정도로, Figure/Table에 캡션 기반 재매칭을 넣기 전과 같은 수준의
    동작이다.

    ⚠️ **"blocks" 단위로는 안 된다 — PyMuPDF가 수식과 다음 문단을 한 블록으로
    합쳐버리는 경우가 있다.** 실측: 어떤 버전에서는 "(1)"과 그 뒤 문단
    ("where ϵ is the Gaussian noise...", 148단어)이 줄바꿈만으로 이어진
    하나의 "blocks" 항목으로 나온다 — 이 블록의 ">20단어=진짜 문단" 판단이
    수식 자신의 위치까지 통째로 삼켜 높이 0짜리 영역이 되고 수식이 사라졌다.
    **줄(line) 단위**(get_text("dict"))로 보면 같은 자리에서 "(1)"이 자기
    줄로 깔끔히 분리돼 있다 — "blocks"는 여러 줄을 하나로 뭉쳐 돌려주지만
    "dict"는 줄마다 자기 bbox를 그대로 준다. 그래서 이 함수는 blocks가 아니라
    줄 단위로 판단한다: 수식 조각 줄(첨자·기호뿐이라 단어 수가 적다)과 진짜
    문단 줄(자연어라 단어 수가 많다)이 단어 수만으로 뚜렷이 갈린다.

    ⚠️ **이 함수가 돌려주는 bbox는 "이미지로 자를 정확한 범위"로만 쓴다.**
    본문에서 이 영역을 제외할 때는 이 bbox로 블록을 완전 포함시키려 하지
    않는다 — 호출부(extract_full_text)가 실제로 갖고 있는 블록은 이보다
    더 크게 뭉쳐 있을 수 있어(위 경고 참고), bbox를 그 블록까지 감싸도록
    늘리면 이번엔 "수식 하나만 찍힌 작은 이미지"가 아니라 "수식+다음
    문단까지 통째로 찍힌 큰 이미지"가 된다(실측). 대신 extract_full_text는
    이 라벨을 포함한 블록을 만나면 그 자리에서 텍스트를 직접 잘라낸다
    (아래 extract_full_text의 해당 처리 참고).
    """
    blocks = page.get_text("blocks")
    page_width = page.rect.width
    right_edge = _page_text_right_edge(blocks, page_width)
    if right_edge is None:
        return []
    # 본문 왼쪽 기준선 — 수식은 가운데 정렬·들여쓰기라 이 기준선에서 뚜렷이
    # 떨어져 있다(_page_text_left_edge 참고). "다음 줄이 진짜 문단인지"를
    # 단어 수만이 아니라 이 기준선에 딱 붙어 시작하는지로도 판단한다.
    left_edge = _page_text_left_edge(blocks, page_width)

    lines: list[tuple[float, float, float, float, str]] = []  # (y0, y1, x0, x1, text)
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type") != 0:
            continue
        for line in blk.get("lines", []):
            text = "".join(span["text"] for span in line["spans"])
            if text.strip():
                bbox = line["bbox"]
                lines.append((bbox[1], bbox[3], bbox[0], bbox[2], text))

    sorted_lines_by_top = sorted(lines, key=lambda ln: ln[0])
    # 이 페이지의 "보통 줄 높이" — 논문마다 폰트 크기·여백이 다르므로 고정
    # pt 값 대신 페이지 자체에서 뽑은 값을 기준으로 삼는다(다른 임계값들과
    # 같은 원칙, _line_height_samples 참고).
    _heights = [ly1 - ly0 for ly0, ly1, _, _, _ in sorted_lines_by_top if ly1 > ly0]
    typical_line_height = statistics.median(_heights) if _heights else 12.0

    # 모든 라벨 줄의 y0를 미리 모아둔다 — 어떤 수식의 연속 확장도 다음
    # 라벨의 시작을 절대 넘지 못하게 하는 하드 경계로 쓴다(아래 참고).
    label_y0s = sorted(
        y0 for y0, _y1, _x0, x1, text in lines
        if _EQUATION_LABEL.match(text.strip()) and abs(x1 - right_edge) <= _EQ_RIGHT_MARGIN_TOLERANCE)

    candidates: list[tuple[float, float, str]] = []
    for y0, y1, _x0, x1, text in lines:
        m = _EQUATION_LABEL.match(text.strip())
        if m and abs(x1 - right_edge) <= _EQ_RIGHT_MARGIN_TOLERANCE:
            next_label_y0 = next((ly for ly in label_y0s if ly > y0), None)
            # ⚠️ **라벨 자기 줄만 쓰면 수식이 잘린다.** 수식은 첨자·루트
            # 기호 때문에 세로로도 들쭉날쭉해서, "(1)" 라벨과 같은 줄에
            # 속한 다른 조각("zt ="처럼)의 bbox가 라벨 자신보다 더 아래까지
            # 내려가는 경우가 흔하다 — 완전 포함 조건에 걸려 이 조각이
            # 본문에 그대로 새어 나온다. 라벨과 세로로 겹치는 줄들 중 가장
            # 아래까지 내려가는 지점을 이 라벨의 실제 하단으로 쓴다.
            row_bottom = y1
            for ly0, ly1, _lx0, lx1, _ltext in lines:
                if left_edge is not None and lx1 <= left_edge:
                    continue  # 왼쪽 여백의 줄번호 — 본문이 아니므로 무시
                if ly0 < y1 and ly1 > y0:
                    row_bottom = max(row_bottom, ly1)
            # ⚠️ **여러 줄짜리 수식은 라벨이 마지막 줄이 아닐 수도 있다**(실측:
            # "max ... / s.t. V = ..." 두 줄짜리 제약식에서 번호가 첫 줄
            # 옆에 찍히고 "s.t." 줄은 그 아래에 옴). 라벨 줄 바로 다음부터
            # 이어지는 줄들도, (a) 다음 문단만큼 간격이 벌어지지 않고
            # (b) 여전히 단어 수가 적어 수식처럼 보이는 동안은 같은 수식의
            # 연속으로 보고 하단을 계속 늘린다.
            #
            # ⚠️ **강조 기호(예: Ŵ의 hat) 때문에 문단 한 줄이 여러 조각으로
            # 쪼개지는 경우가 있다.** 실측: "On the Update of Ŵ. To update
            # Ŵ, we rely on..."이라는 한 문장이 "On the Update of c" / "To
            # update c" / "W ." / "W , we rely..." 4조각으로 쪼개져 나오면,
            # 조각마다 단어 수가 6개 이하라 전부 "수식 조각"으로 오인되고
            # 문단 전체가 수식 연속으로 삼켜진다. 단어 수 조건 하나만으로는
            # 쪼개진 조각을 못 잡으므로, **이 줄이 본문 왼쪽 기준선에 딱
            # 붙어 시작하는지**를 같이 본다 — 수식은 항상 들여쓰기돼 있고
            # (실측: 본문 108.0 vs 수식 조각 150~230대) 진짜 문단은 쪼개진
            # 조각이라도 그 첫 조각은 반드시 왼쪽 기준선에서 시작하므로, 이
            # 신호는 단어 수와 무관하게 강하게 작동한다. 이 "진짜 문단"
            # 판정은 오탐 방지 상한(extended cap)보다 먼저 확인한다 — 안
            # 그러면 짧은 조각 여러 개가 먼저 상한을 채워버려서 정작 그
            # 다음에 오는 진짜 문단 신호를 볼 기회조차 없이 이미 멈춰버린다.
            #
            # ⚠️ **리뷰용 줄번호(왼쪽 여백의 "197", "198" 등)가 다음 수식
            # 라벨을 건너뛰게 만든다.** 이 줄번호들은 본문 칼럼보다도 왼쪽
            # 여백(왼쪽 기준선보다 더 왼쪽)에 있어 원래는 이 판단과 무관해야
            # 하는데, 예외로 취급돼 계속 row_bottom을 늘리는 데 쓰인다.
            # 실측: 줄번호가 다음 수식 "(4)"의 라벨 줄보다 먼저 처리되며
            # row_bottom을 그 라벨 위치 너머로 밀어버려서, 정작 "(4)" 자신은
            # `ly0 < row_bottom`에 걸려 라벨 판정조차 받지 못하고 조용히
            # 건너뛰어진다 — 그러면 수식 (4) 전체가 (3)의 연속으로 삼켜진다.
            # 여백 줄번호는 본문 내용이 전혀 아니므로 row_bottom·확장 횟수
            # 어느 쪽에도 관여하지 않도록 아예 건너뛴다.
            #
            # ⚠️ **단어 수·들여쓰기 판정만으로는 다음 수식 자신의 공식
            # 조각까지 삼킬 수 있다.** 실측: "(4)"의 자기 수식("U (t), V
            # (t) := argmin...")도 첨자 때문에 여러 조각으로 쪼개지는데, 그
            # 조각들 역시 단어 수가 적고 들여쓰기돼 있어(수식이니 당연히)
            # "진짜 문단 아님"으로 판정돼 (3)의 연속 확장이 이 조각들을
            # 계속 삼키다가 (4) 자신의 라벨 줄에 닿기도 전에 확장 상한에
            # 걸려버린다 — (3)의 하단이 (4)의 라벨보다 아래로 밀려 (4)의
            # 영역이 음수 높이가 된다. 다음 라벨의 y0를 미리 알고 있으므로,
            # 어떤 확장도 그 지점을 절대 넘지 못하게 하드 상한선을 둔다.
            extended = 0
            for ly0, ly1, lx0, lx1, ltext in sorted_lines_by_top:
                if ly0 < row_bottom:
                    continue
                if next_label_y0 is not None and ly1 > next_label_y0:
                    break  # 다음 수식의 영역까지 걸치는 줄 — 절대 넘어가지 않는다
                if left_edge is not None and lx1 <= left_edge:
                    continue  # 왼쪽 여백의 줄번호 — 본문이 아니므로 무시
                if ly0 - row_bottom > typical_line_height * 1.5:
                    break  # 다음 문단만큼 간격이 벌어짐
                if _EQUATION_LABEL.match(ltext.strip()):
                    break  # 다음 수식 라벨 — 그 수식은 별도로 처리되므로 넘어오면 안 된다
                ltoks = ltext.split()
                is_ltoks_line_numbers = ltoks and all(_LINE_NUMBER.fullmatch(t) for t in ltoks)
                is_flush_left = abs(lx0 - left_edge) <= _EQ_LEFT_MARGIN_TOLERANCE if left_edge is not None else False
                if not is_ltoks_line_numbers and (
                        is_flush_left or len(ltoks) > _EQ_PROSE_LINE_MIN_WORDS):
                    break  # 진짜 문단이 다시 시작됨
                if extended >= _EQ_CONTINUATION_MAX_LINES:
                    break  # 오탐 방지 상한 — 여기까지 왔으면 더는 안 늘린다
                row_bottom = max(row_bottom, ly1)
                extended += 1
            candidates.append((y0, row_bottom, m.group(1)))
    if not candidates:
        return []
    candidates.sort(key=lambda c: c[0])

    sorted_lines = sorted(lines, key=lambda ln: ln[0])
    regions: list[tuple[float, float, float, float, str]] = []
    last_prose_y1 = 0.0
    li = ci = 0
    while li < len(sorted_lines) or ci < len(candidates):
        next_line_y = sorted_lines[li][0] if li < len(sorted_lines) else float("inf")
        next_cand_y = candidates[ci][0] if ci < len(candidates) else float("inf")
        if next_cand_y <= next_line_y:
            top, bottom, num = candidates[ci]
            regions.append((last_prose_y1, bottom, 0.0, page_width, f"Equation {num}"))
            last_prose_y1 = bottom
            ci += 1
        else:
            ly0, ly1, lx0, _lx1, ltext = sorted_lines[li]
            tokens = ltext.split()
            is_line_numbers = tokens and all(_LINE_NUMBER.fullmatch(t) for t in tokens)
            is_flush_left = left_edge is not None and abs(lx0 - left_edge) <= _EQ_LEFT_MARGIN_TOLERANCE
            if not is_line_numbers and (is_flush_left or len(tokens) > _EQ_PROSE_LINE_MIN_WORDS):
                last_prose_y1 = ly1
            li += 1

    return regions


# 표 괘선 후보로 볼 최소 폭(페이지 폭 대비 비율). 값 하드코딩 대신 비율로 두어
# 페이지 크기가 다른 문서에도 그대로 적용된다. 단어 밑줄·강조선처럼 짧은 선은
# 이 비율보다 훨씬 좁다 — 실측(아래 _table_regions 참고): 표 괘선 340~360pt대
# vs 일반 텍스트 밑줄류는 이 폭에 한참 못 미친다.
_MIN_RULE_WIDTH_RATIO = 0.15


def _horizontal_rules(page) -> list[float]:
    """페이지의 가로 벡터선(표 괘선 후보)의 y좌표 목록.

    PyMuPDF page.get_drawings()의 순수 가로선은 rect.height == 0으로 나온다
    (세로선은 반대로 width == 0). 폭이 좁은 것(글자 강조선 등)은 제외한다.
    """
    page_width = page.rect.width
    min_width = page_width * _MIN_RULE_WIDTH_RATIO
    out = []
    for d in page.get_drawings():
        rect = d.get("rect")
        if rect is None or rect.height != 0:
            continue
        if rect.width >= min_width:
            out.append(rect.y0)
    return out


def _repeating_rule_ys(doc) -> frozenset[float]:
    """문서 대부분의 페이지에 똑같은 y좌표로 찍히는 가로선의 좌표 집합.

    머리말/꼬리말을 문구가 아니라 반복되는 정도로 찾는 것(extract_full_text의
    repeating 집합)과 같은 원리를 괘선에도 적용한다. 실측: 학회 템플릿이
    모든 페이지 맨 위에 고정 y좌표로 장식용 가로선을 하나씩 찍는데, 이 선이
    걸러지지 않으면 캡션과 우연히 가까운 "위쪽 괘선"으로 뽑혀 _table_regions의
    위/아래 괘선 선택 로직(더 가까운 쪽을 진짜로 봄)이 알고리즘 박스의 진짜
    경계 대신 이 장식선을 골라버린다 — 그러면 알고리즘 영역이 캡션 한 줄
    높이로 쪼그라들고, 정작 그 아래 의사코드 본문은 크롭도 안 되고 일반
    텍스트로 새어 나온다.
    """
    n_pages = doc.page_count
    counter: dict[float, int] = {}
    for pno in range(n_pages):
        for y in {round(v, 1) for v in _horizontal_rules(doc[pno])}:
            counter[y] = counter.get(y, 0) + 1
    return frozenset(y for y, c in counter.items() if n_pages >= 3 and c >= max(3, n_pages * 0.3))


def _table_regions(page, excluded_rule_ys: frozenset = frozenset()) -> list[tuple[float, float, float, float, str]]:
    """페이지에서 표·알고리즘 영역을 (region_top, region_bottom, x0, x1, label)로 찾는다.

    그림과 반대로 표·알고리즘은 캡션이 위에 오는 게 관례라, "캡션 앞 마지막
    문단"이 아니라 **find_tables()가 찾은 실제 표 grid의 bbox**를 그대로 쓴다
    — "짧은 블록이면 표 셀일 것"이라는 word-count 추정(extract_full_text의
    기존 skip_table_cells)은 여러 행이 한 블록에 뭉쳐 8단어를 넘기면 못
    잡는다(실측: 표 데이터 행이 그대로 새 문단으로 새서, 문장부호 없이
    끝나는 바람에 _stitch_split_sentences가 다음 문장에 잘못 갖다 붙였다).
    실제 grid 경계를 쓰면 이 문제를 구조적으로 피한다. Algorithm(의사코드)은
    grid가 아니라 괘선만 있는 게 보통이라 find_tables() 매칭 대상에서는
    빼고, 아래 괘선 기반 판단으로만 잡는다.

    캡션은 기본적으로 "바로 위(문서 순서상 가장 가까운 이전)"를 표에
    매칭하지만, 위/아래 어느 쪽이 실제로 더 가까운지 그룹 단위로 비교해서
    정한다(실측: 모델별 결과를 이어붙인 표 하나가 find_tables()에 여러
    조각으로 쪼개져 잡히는데, 캡션이 관례와 달리 조각들의 맨 아래에 있는
    경우가 있다 — 자세한 근거는 아래 그룹핑 설명 참고). extract_full_text와
    extract_media_images가 이 함수 하나를 공유해서 "이 표가 몇 번인지"를
    서로 다르게 판단하지 않는다.

    ⚠️ **find_tables()가 표 하나를 여러 조각으로 쪼개 감지할 수 있다**(실측:
    "Table 6" 하나가 세 개의 별도 grid로 잡혀, 셋 다 같은 캡션에 매칭됨).
    같은 라벨로 매칭된 영역은 하나로 합친다(top/bottom을 감싸는 범위로) —
    안 그러면 본문에서 같은 표 placeholder가 여러 번 나타나 diff가 그
    표를 "바뀐 것"으로 잘못 보게 된다.

    ⚠️ **find_tables()는 완전히 동일한 표에도 들쭉날쭉하다**(실측: 벡터선
    좌표까지 바이트 단위로 동일한 "Table 6"이, 같은 페이지에 다른 내용이
    더 있고 없고에 따라 한 리비전에서는 grid 0개, 다른 리비전에서는 3개로
    잡혔다 — 표 자체는 안 바뀌었는데 diff가 "본문이 표로 바뀌었다"로
    오인하는 원인이 됐다). find_tables()가 못 찾은 캡션은 **괘선 개수**로
    한 번 더 판단한다: LaTeX booktabs류(세로선 없이 위/아래 가로 괘선만 있는
    표·algorithmic 패키지의 의사코드 박스)는 캡션 바로 아래에 폭 넓은
    가로선이 최소 2개(위/아래 테두리) 있다. 다음 캡션 전까지 그런 가로선이
    2개 이상 있으면 그 사이를 영역으로 본다 — 문구나 이 논문에 특화하지
    않고 순수 기하 정보만 쓴다.
    """
    try:
        found = page.find_tables()
    except Exception as exc:
        log.warning("표 영역 인식 실패: %s", exc)
        found = None
    all_captions = []
    grid_captions = []   # find_tables()의 grid 매칭 대상 — 실제 표만. (x0,y0,x1,y1,label)
    ruled_captions = []  # 괘선 기반 판단 대상 — 표 + 알고리즘(둘 다 캡션이 위)
    all_blocks = _ordered_blocks(page)
    for b in all_blocks:
        if b[6] != 0:
            continue
        text = _flatten_block_text(b[4])
        m = _CAPTION.match(text)
        if m:
            kind = m.group(1).lower()
            entry = (b[0], b[1], b[2], b[3], f"{kind.title()} {m.group(2)}")
            all_captions.append(entry)
            if kind == "table":
                grid_captions.append(entry)
                ruled_captions.append(entry)
            continue
        m2 = _ALGO_CAPTION.match(text)
        if m2:
            # 아직 캡션이라고 확정하지 않는다 — 바로 아래 괘선 개수(뒤의
            # 반복문)로 최종 확인한다. 여기서는 후보로만 등록한다.
            entry = (b[0], b[1], b[2], b[3], f"Algorithm {m2.group(1)}")
            all_captions.append(entry)
            ruled_captions.append(entry)
    def _x_overlap(a_x0: float, a_x1: float, b_x0: float, b_x1: float) -> float:
        return max(0.0, min(a_x1, b_x1) - max(a_x0, b_x0))

    # ⚠️ **한 페이지에 "캡션이 아래에 오는" 표가 여러 개 연달아 있으면
    # bbox 하나하나 따로 캡션을 찾아선 안 된다**(실측: 모델별 결과를 이어붙인
    # 표 두 개(Table 2, Table 3)가 한 페이지에 나란히 있는데, Table 3의
    # 조각들이 Table 2의 캡션에 더 가까워 보여(위쪽에 있으니) 잘못 거기
    # 붙는다 — 실제로는 각 조각 그룹 바로 "다음"에 오는 자기 캡션이 훨씬
    # 가깝다). 그래서 먼저 bbox들을 "사이에 캡션이 끼어 있지 않으면 같은
    # 표"로 묶고(끼어 있으면 그 캡션이 앞 그룹 것이므로 그룹이 끊긴다),
    # 그룹 단위로 위/아래 중 실제 간격이 더 가까운 캡션을 찾는다.
    #
    # ⚠️ **표 두 개가 세로로 쌓인 게 아니라 가로로 나란히 있을 수도 있다**
    # (실측: "Table 5"·"Table 6"이 한 페이지에 좌우로 나란히 있고 캡션도
    # 좌우로 나란히 있어, y좌표만 보면 완전히 같은 높이다 — 세로 순서 기준
    # 그룹핑은 이 둘을 "같은 표"로 잘못 합친다). y순으로 정렬해 다음 bbox와
    # 묶을지 판단할 때, 가로로 겹치지 않으면(나란히 있는 다른 표) 무조건
    # 새 그룹으로 끊는다 — 세로로 쌓인 조각(같은 표)은 보통 가로 범위가
    # 거의 같으므로 겹침 판단이 자연스럽게 맞다.
    page_width = page.rect.width
    sorted_tables = sorted((found.tables if found is not None else []), key=lambda t: t.bbox[1])
    groups: list[list[tuple[float, float, float, float]]] = []
    for t in sorted_tables:
        x0, top, x1, bottom = t.bbox
        if groups:
            prev_group = groups[-1]
            prev_bottom = max(b for _, _, _, b in prev_group)
            prev_x0 = min(gx0 for gx0, _, _, _ in prev_group)
            prev_x1 = max(gx1 for _, _, gx1, _ in prev_group)
            has_intervening_caption = any(
                prev_bottom < cy0 < top for _cx0, cy0, _cx1, _cy1, _label in grid_captions)
            x_overlaps = _x_overlap(prev_x0, prev_x1, x0, x1) > 0
            if has_intervening_caption or not x_overlaps:
                groups.append([])
        else:
            groups.append([])
        groups[-1].append((x0, top, x1, bottom))

    # (top, bottom, x0, x1) — x0/x1은 find_tables()가 준 실제 grid 가로 범위를
    # 그대로 쓴다. 표가 페이지 폭 일부만 차지하고 옆에 본문 텍스트가 계속
    # 이어지는 레이아웃(그림과 마찬가지 문제)에서도, 실제 표 폭만큼만 크롭·
    # 제외 대상으로 삼기 위함 — 텍스트 흐름 추정이 아니라 find_tables()가
    # 이미 갖고 있는 진짜 좌표를 버리지 않고 살린다.
    # find_tables()가 표 하나를 "표"로 인정하는 grid 좌표에는 컬럼 헤더 줄
    # (예: "Method Model PPL ...")이 안 들어가 있을 수 있다(실측: 헤더 줄이
    # 테두리·괘선에 안 붙어 있어 find_tables()가 표의 일부로 안 봄). 그러면
    # group_top이 실제보다 아래에 잡혀 헤더 줄이 본문에 그대로 새어 나온다
    # — 그리드 바로 위(작은 간격)에 가로로 겹치는 텍스트 블록이 있으면
    # 헤더 줄로 보고 상단 경계를 그 블록 시작까지 끌어올린다. 간격을 좁게
    # 잡아(줄 높이의 1.5배) 진짜 앞 문단(문단 사이 여백이 더 넓다)까지
    # 잘못 끌어들이지 않게 한다.
    # ⚠️ all_blocks는 여러 줄이 뭉친 병합 블록이라 그 높이를 "줄 높이"로
    # 쓰면 안 된다(실측: 문단 블록 하나가 50pt 넘게 커서 간격 상한이
    # 과하게 커지고, 페이지 상단 러닝헤드 "Under review as a conference
    # paper..."까지 표 헤더로 잘못 끌어들였다). get_text("dict")의 개별
    # 줄(line) bbox로 진짜 한 줄 높이를 재야 한다.
    _line_heights = []
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type") != 0:
            continue
        for line in blk.get("lines", []):
            lh = line["bbox"][3] - line["bbox"][1]
            if lh > 0:
                _line_heights.append(lh)
    _typical_line_height = statistics.median(_line_heights) if _line_heights else 12.0
    _HEADER_GAP_LIMIT = _typical_line_height * 1.5

    merged: dict[str, tuple[float, float, float, float]] = {}
    for group in groups:
        group_top = min(top for _, top, _, _ in group)
        group_bottom = max(bottom for _, _, _, bottom in group)
        group_x0 = min(x0 for x0, _, _, _ in group)
        group_x1 = max(x1 for _, _, x1, _ in group)
        header_candidates = [
            b for b in all_blocks
            if b[6] == 0 and b[3] <= group_top and group_top - b[3] <= _HEADER_GAP_LIMIT
            and _x_overlap(b[0], b[2], group_x0, group_x1) > 0
            and not _CAPTION.match(_flatten_block_text(b[4]))
        ]
        if header_candidates:
            group_top = min(group_top, min(b[1] for b in header_candidates))
        # 캡션이 여러 개 나란히 있을 때(위 표 나란히 배치와 같은 이유) 순전히
        # 세로 간격만으로 고르면 옆 칸 캡션을 잘못 집을 수 있다 — 가로로
        # 겹치는 캡션을 먼저 걸러내고, 하나도 안 겹치면(드문 경우, 캡션이
        # 표보다 살짝 넓거나 좁을 수 있어 방어적으로) 전체를 후보로 되돌린다.
        def _filter_by_x_overlap(cands):
            overlapping = [c for c in cands if _x_overlap(c[0], c[2], group_x0, group_x1) > 0]
            return overlapping or cands

        candidates = []
        above = _filter_by_x_overlap(
            [(cx0, cy0, cx1, cy1, label) for cx0, cy0, cx1, cy1, label in grid_captions if cy1 <= group_top])
        if above:
            cx0, cy0, cx1, cy1, label = max(above, key=lambda c: c[3])  # 바로 위 캡션
            candidates.append((group_top - cy1, cy0, cy1, label))
        below = _filter_by_x_overlap(
            [(cx0, cy0, cx1, cy1, label) for cx0, cy0, cx1, cy1, label in grid_captions if cy0 >= group_bottom])
        if below:
            cx0, cy0, cx1, cy1, label = min(below, key=lambda c: c[1])  # 바로 아래 캡션
            candidates.append((cy0 - group_bottom, cy0, cy1, label))
        if not candidates:
            continue
        _gap, cap_y0, cap_y1, label = min(candidates, key=lambda x: x[0])
        region_top, region_bottom = min(group_top, cap_y0), max(group_bottom, cap_y1)
        if label in merged:
            prev_top, prev_bottom, prev_x0, prev_x1 = merged[label]
            merged[label] = (min(prev_top, region_top), max(prev_bottom, region_bottom),
                             min(prev_x0, group_x0), max(prev_x1, group_x1))
        else:
            merged[label] = (region_top, region_bottom, group_x0, group_x1)

    # ⚠️ **괘선 표는 캡션이 위일 수도, 아래일 수도 있다.** 예전엔 캡션
    # 아래쪽만 찾았는데(booktabs 표가 "캡션 → 괘선" 순서라고만 가정) 실측
    # 결과 같은 논문 안에서도 "괘선 → 캡션"(캡션이 표 아래) 순서가 흔했다
    # (Table 4, Table 5가 나란히 이 순서였다). 아래쪽만 보면 다음 캡션까지의
    # 구간에서 **다음 표 자신의 괘선**을 엉뚱하게 주워버리고(실측: Table
    # 4가 자기 괘선 대신 Table 5의 괘선을 삼킴), 정작 그 다음 표(Table 5)는
    # 자기 괘선이 이미 위쪽에서 다 소진돼 하나도 못 찾아 영역 자체가
    # 안 잡혔다. 위/아래 둘 다 찾아보고, 캡션에 더 가까운(간격이 작은)
    # 쪽을 진짜 자기 괘선으로 본다 — 위/아래 양쪽 다 확실히 표를 못 찾는
    # 경우 없이 두 방향을 대칭으로 다룬다.
    #
    # ⚠️ **같은 y좌표의 괘선이 여러 번 잡힐 수 있다** — 알고리즘 두 개가
    # 페이지에 좌우로 나란히 놓이면(실측: "Algorithm 1"·"Algorithm 2"가
    # 나란히 배치) 각자의 테두리가 같은 높이에 그려져 사실상 괘선 하나인데
    # _horizontal_rules는 좌/우 도형을 따로 두 개로 센다. 중복 제거 없이
    # "괘선 ≥2개"를 요구하면, 캡션 바로 위의 테두리 하나가 좌우로 겹쳐
    # 2개로 잘못 세져 "위쪽에 진짜 표/캡션이 하나 더 있다"고 오인하고,
    # 캡션 바로 아래로 한참 이어지는 진짜 알고리즘 본문(줄 번호가 매겨진
    # 의사코드)을 놓친다. y좌표를 반올림해 중복을 지우고 나서 세야 한다.
    rules = sorted({round(y, 1) for y in _horizontal_rules(page)} - excluded_rule_ys)
    page_bottom = page.rect.height
    for _cap_x0, cap_y0, _cap_x1, cap_y1, label in ruled_captions:
        if label in merged:
            continue
        later_caption_tops = [y0 for _x0, y0, _x1, _y1, lbl in all_captions if y0 > cap_y0 and lbl != label]
        earlier_caption_bottoms = [y1 for _x0, _y0, _x1, y1, lbl in all_captions if y1 < cap_y0 and lbl != label]
        next_cap_top = min(later_caption_tops) if later_caption_tops else None
        limit_below = next_cap_top if next_cap_top is not None else page_bottom
        floor_above = max(earlier_caption_bottoms) if earlier_caption_bottoms else 0.0
        # ⚠️ **알고리즘 박스는 캡션 바로 위에 자기 테두리 괘선을 하나 더
        # 가진다**(실측: "Algorithm 1"류 캡션 1~2pt 위에 항상 테두리선이
        # 있음 — algorithmic 패키지의 관례). 두 알고리즘이 한 페이지에
        # 붙어 있으면(실측: Algorithm 3 바로 아래 Algorithm 4), 앞
        # 알고리즘의 "below" 탐색 구간이 다음 캡션 시작 직전까지 뻗어
        # 있어서, 사실은 **다음 알고리즘 자신의 테두리선**인 그 괘선을
        # 마지막 괘선(below[-1])으로 잘못 주워버린다 — 그러면 앞
        # 알고리즘 영역이 자기 진짜 종료선보다 한참 아래(다음 캡션
        # 코앞)까지 부풀고, 정작 뒤 알고리즘은 자기 시작 테두리를 이미
        # 빼앗겨 캡션 한 줄 높이로 쪼그라든다. 다음 캡션 바로 위(줄 높이의
        # 1.5배 이내)에 붙어 있는 괘선은 "다음 캡션 소유"로 보고 이번
        # below 후보에서 제외한다.
        below = sorted(y for y in rules if cap_y0 <= y <= limit_below
                       and (next_cap_top is None or next_cap_top - y > _HEADER_GAP_LIMIT))
        # ⚠️ **알고리즘은 캡션이 항상 내용보다 위에 온다** — algorithmic
        # 패키지 관례상 "Algorithm N ..." 캡션 자체가 박스의 첫 줄이고,
        # 의사코드는 늘 그 아래로 이어진다(표처럼 캡션이 위/아래 둘 다일
        # 수 있는 것과 다르다). 그런데 이 캡션 바로 위에도 박스 자신의
        # 테두리 괘선이 붙어 있어(위 주석 참고) 간격이 아주 작게 나온다 —
        # "위/아래 중 캡션과 더 가까운 쪽" 비교를 그대로 적용하면 이 위쪽
        # 테두리(진짜 내용이 없는 장식선)가 매번 이기고, 정작 아래로 쭉
        # 이어지는 진짜 의사코드 본문을 캡션 한 줄 높이로 잘라버린다(실측:
        # 알고리즘이 한 페이지에 두 개 이상 붙어 있을 때마다 재현). 그래서
        # 알고리즘 캡션은 위/아래 비교 없이 항상 아래쪽만 본다.
        is_algorithm = label.startswith("Algorithm")
        above = [] if is_algorithm else sorted(y for y in rules if floor_above <= y <= cap_y0)
        below_gap = (below[0] - cap_y0) if len(below) >= 2 else None
        above_gap = (cap_y0 - above[-1]) if len(above) >= 2 else None
        # 괘선만으로는 가로 범위를 알 수 없다(가로선 자체가 페이지 폭 근처까지
        # 그려지는 경우가 많아 폭 판단에 못 쓴다) — 페이지 전체 폭으로 둔다
        # (지금까지의 동작과 동일, 회귀 없음).
        if below_gap is not None and (above_gap is None or below_gap <= above_gap):
            # ⚠️ **알고리즘은 "다음 캡션까지"가 아니라 "다음 괘선까지"로
            # 종료선을 잡아야 한다.** 표는 below[-1](다음 캡션 직전 마지막
            # 괘선)이 곧 자기 grid의 닫는 선이지만, 알고리즘은 페이지에
            # 다음 캡션이 없으면(실측: 그 알고리즘이 그 페이지의 마지막
            # 캡션) limit_below가 페이지 끝까지 뻗어서, 그 사이에 있는
            # 전혀 무관한 뒷부분(실측: 새 절 제목, 별도 캡션 없는 코드
            # Listing)의 괘선까지 다 below에 잡혀 below[-1]이 알고리즘
            # 자신의 종료선보다 훨씬 아래를 가리킨다. 알고리즘 박스는 항상
            # [캡션 직후 구분선(below[0]), 종료선(below[1])] 2개만 자기
            # 것이므로 below[1]을 쓴다 — 표는 종전대로 below[-1](여러
            # 조각을 감싸는 진짜 마지막 선)을 그대로 쓴다.
            bottom = below[1] if is_algorithm else below[-1]
            merged[label] = (cap_y0, bottom, 0.0, page_width)
        elif above_gap is not None:
            merged[label] = (above[0], cap_y1, 0.0, page_width)

    return [(top, bottom, x0, x1, label) for label, (top, bottom, x0, x1) in merged.items()]


# 박스(코드·프롬프트 인용 등)로 볼 채우기 사각형의 최소 크기. 페이지 폭 대비
# 비율(고정 pt 대신)로 문서 크기와 무관하게 적용되고, 높이는 강조 밑줄 한 줄과
# 확실히 구분되는 값으로 잡는다.
_MIN_BOX_WIDTH_RATIO = 0.5
_MIN_BOX_HEIGHT = 20.0


def _merged_box_rects(page) -> list:
    """박스 후보 채우기(fill) 사각형을 찾아 겹치거나 맞닿은 것끼리(테두리+배경
    두 겹) 합친 rect 목록. LaTeX tcolorbox/mdframed류는 진한 테두리색 사각형
    위에 옅은 배경색 사각형을 겹쳐 그리는 방식으로 박스를 만든다(실측) —
    page.get_drawings()의 채우기 도형 중 페이지 폭의 절반 이상, 높이 20pt
    이상인 것을 후보로 본다(강조 밑줄 한 줄과 구분).

    _box_regions와 extract_box_texts가 이 함수 하나를 공유해서 두 곳이 서로
    다른 rect를 기준으로 계산해 어긋나는 일(해시가 본 텍스트와 유사도 비교가
    본 텍스트가 다른 영역이 되는 것)이 없게 한다.
    """
    import fitz  # PyMuPDF

    page_width = page.rect.width
    min_width = page_width * _MIN_BOX_WIDTH_RATIO
    rects = []
    for d in page.get_drawings():
        if d.get("type") != "f":
            continue
        rect = d.get("rect")
        if rect is None or rect.width < min_width or rect.height < _MIN_BOX_HEIGHT:
            continue
        rects.append(fitz.Rect(rect))
    if not rects:
        return []

    rects.sort(key=lambda r: r.y0)
    merged = [rects[0]]
    for r in rects[1:]:
        prev = merged[-1]
        if r.y0 <= prev.y1 + 1:
            merged[-1] = fitz.Rect(min(prev.x0, r.x0), min(prev.y0, r.y0),
                                    max(prev.x1, r.x1), max(prev.y1, r.y1))
        else:
            merged.append(r)
    return merged


def _box_regions(page, exclude: list[tuple[float, float]]) -> list[tuple[float, float, float, float, str]]:
    """캡션 없이 배경색 박스로만 구분되는 영역(코드·프롬프트 인용 등)을 찾는다.

    Figure/Table/Algorithm처럼 "N번" 캡션이 있는 것들과 달리, 이런 박스는
    저자가 붙인 번호가 없다.

    ⚠️ **번호 대신 내용 해시로 식별한다.** 순서(몇 번째 박스인지)로 이름을
    붙이면 앞에 박스가 하나만 추가돼도 뒤의 모든 박스가 "바뀐 것"으로
    오인된다(Figure/Table은 저자가 직접 번호를 다시 매기므로 이 문제가 없지만,
    무기명 박스는 우리가 임의로 매기는 순번이라 위치가 흔들리면 그대로
    깨진다). 내용이 같으면 버전이 달라도 같은 라벨이 나와 diff가 "안 바뀜"으로
    정확히 인식하고, 내용이 다르면 다른 라벨이라 자연히 삭제+삽입 쌍으로
    보인다 — 해시 자체는 _box_content_hash가 만든다.

    exclude: 이미 표/알고리즘/그림으로 인식된 영역(top, bottom) 목록. 옅은
    줄무늬 배경(zebra striping)을 쓰는 표까지 박스로 오인하지 않도록 겹치면
    뺀다. **그림도 반드시 포함해야 한다** — 그림 자체의 배경 패널(다이어그램
    강조용 색칠 사각형)이 페이지 폭 절반 이상·20pt 이상이면 그림과 무관한
    별도의 "박스"로 오인된다(실측: Figure가 있는 페이지마다 예외 없이 그림
    영역 안에 완전히 포함되는 박스 사각형이 잡혔다 — 그림의 배경 패널을
    캡처한 것). 이러면 문장 한가운데 "(박스 …)"와 "(그림 N)"이 나란히 끼어
    들어가(실측: "It can be (박스 …) (그림 5) also seen that..." 처럼 문장이
    두 조각으로 쪼개짐) 원문에 없던 문단 경계가 생긴다.
    """
    regions = []
    for rect in _merged_box_rects(page):
        if any(top <= rect.y0 and rect.y1 <= bottom for top, bottom in exclude):
            continue
        digest = _box_content_hash(page, rect)
        regions.append((rect.y0, rect.y1, rect.x0, rect.x1, f"Box {digest}"))
    return regions


def _box_text(page, rect) -> str:
    """박스 영역 안의 정규화된 텍스트. 비어 있으면 텍스트 레이어가 없는 순수
    이미지 박스라는 뜻이다. 줄번호 여백 숫자는 본문 추출과 같은 기준
    (_LINE_NUMBER)으로 제외한다.
    """
    parts = []
    for b in page.get_text("blocks", clip=rect):
        if b[6] != 0:
            continue
        t = b[4].strip()
        if not t:
            continue
        tokens = t.split()
        if all(_LINE_NUMBER.fullmatch(tok) for tok in tokens):
            continue
        parts.append(t)
    return " ".join(" ".join(parts).split())


def _box_content_hash(page, rect) -> str:
    """박스 영역의 내용 해시. 텍스트가 있으면 텍스트를, 없으면(순수 이미지 등
    텍스트 레이어가 비는 경우) 픽셀을 해시한다.

    ⚠️ **픽셀 해시만 쓰면 페이지 절대 위치에 흔들린다**(실측: 리비전 사이에
    같은 프롬프트 박스가 다른 페이지의 다른 y좌표로 밀렸을 뿐인데, 박스
    자체의 폭·높이는 소수점까지 동일한데도 다른 해시가 나왔다 — PyMuPDF가
    클립 사각형을 픽셀 격자에 반올림하는 계산이 절대좌표에 딸려 있어 세로
    1px 차이가 생겼다). 텍스트는 페이지 내 절대 위치와 무관하고, 내용이
    조금이라도 바뀌면 자연히 다른 해시가 나와 원래 의도(내용 기준 식별)에
    더 맞는다.
    """
    normalized = _box_text(page, rect)
    if normalized:
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
    pix = page.get_pixmap(clip=rect, dpi=24)  # 텍스트가 없을 때만 쓰는 폴백
    return hashlib.sha1(pix.samples).hexdigest()[:10]


def extract_box_texts(pdf_bytes: bytes, max_pages: int = MAX_BODY_PAGES) -> dict[str, str]:
    """{"Box <해시>": 내용 텍스트}. 텍스트가 비어 있으면(순수 이미지 박스) 키
    자체를 만들지 않는다 — 호출자가 없는 키를 "비교 재료 없음"으로 판단한다.

    같은 해시를 가진(=완전히 동일한) 박스는 attach_body_diffs가 이미 하나로
    합쳐서 다루므로, 서로 다른 해시를 가진 박스끼리 "완전히 다른 박스"인지
    "같은 박스가 살짝 수정된 것"인지 판단할 유사도 비교 재료로 쓴다.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if doc.page_count > max_pages:
            return {}
        out: dict[str, str] = {}
        excluded_rule_ys = _repeating_rule_ys(doc)
        for pno in range(doc.page_count):
            page = doc[pno]
            table_regions = _table_regions(page, excluded_rule_ys)
            table_bottoms = tuple(bottom for _, bottom, _, _, _ in table_regions)
            figure_regions = _figure_regions(page, _ordered_blocks(page), table_bottoms)
            exclude = [(top, bottom) for top, bottom, _, _, _ in table_regions + figure_regions]
            for rect in _merged_box_rects(page):
                if any(top <= rect.y0 and rect.y1 <= bottom for top, bottom in exclude):
                    continue
                text = _box_text(page, rect)
                if text:
                    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
                    out[f"Box {digest}"] = text
        return out
    finally:
        doc.close()


def extract_media_captions(pdf_bytes: bytes, max_pages: int = MAX_BODY_PAGES) -> dict[str, str]:
    """{"Figure N"/"Table N"/"Algorithm N": 캡션 설명문(번호 뗀 나머지)}.

    번호가 재배치돼도(예: Figure 5 → Figure 7) 설명 문구 자체는 그대로인
    경우가 많다 — attach_body_diffs가 이 텍스트로 "번호만 다를 뿐 같은
    그림"을 재매칭한다(_rematch_media_labels). **이미지 픽셀 유사도보다
    캡션 문구가 훨씬 믿을 만하다**: 실측으로 같은 그림이 다른 페이지
    위치로 옮겨가면(주변 레이아웃이 달라져 크롭 여백이 달라짐) 픽셀
    유사도가 0.887까지 떨어지는데, 완전히 다른 두 그림의 픽셀 유사도가
    0.96까지 나오는 경우도 있어 임계값으로 구분이 안 된다. 반면 캡션
    문구는 같은 그림이면 1.0, 다른 그림이면 0.05~0.09 수준으로 뚜렷이
    갈린다(번호 앞자리만 다르고 나머지 설명은 저자가 그대로 재사용하기
    때문 — 실측: "Failure cases. The first two examples..."가 번호만
    바뀐 채 그대로 재사용됨).
    """
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if doc.page_count > max_pages:
            return {}
        out: dict[str, str] = {}
        for pno in range(doc.page_count):
            page = doc[pno]
            for b in _ordered_blocks(page):
                if b[6] != 0:
                    continue
                text = _flatten_block_text(b[4])
                m = _CAPTION.match(text)
                if m:
                    out[f"{m.group(1).title()} {m.group(2)}"] = text[m.end():].strip(" :.")
                    continue
                m2 = _ALGO_CAPTION.match(text)
                if m2:
                    out[f"Algorithm {m2.group(1)}"] = text[m2.end():].strip(" :.")
        return out
    finally:
        doc.close()


def _line_height_samples(blocks: list) -> list[float]:
    """블록 안에 줄이 2개 이상이면 (블록 높이 / 줄 수)로 한 줄 높이를 추정한다.

    문서마다 폰트 크기가 달라 고정 pt 값을 쓸 수 없다 — 문서 자체에서 표본을
    뽑아 중앙값을 기준 삼는다(_merge_close_text_blocks 참고).
    """
    out = []
    for b in blocks:
        if b[6] != 0:
            continue
        lines = b[4].count("\n") + 1
        height = b[3] - b[1]
        if lines >= 2 and height > 0:
            out.append(height / lines)
    return out


def _flatten_block_text(text: str) -> str:
    """블록 안의 줄바꿈을 지운다 — PDF 자체 컬럼 폭에서 줄이 바뀐 자리일 뿐,
    문단 구조와는 무관하다. 그대로 두면 화면 폭이 PDF 컬럼 폭과 다를 때
    문장 중간에서 또 줄바꿈되는 것처럼 보인다(문단 간 구분은 상위에서
    "\\n\\n"으로 따로 넣으므로 여기서 지워도 문단 경계는 안 없어진다).

    줄 끝 하이픈("opera-\\ntor")은 하이픈을 떼고 붙인다 — _join_spans(제목
    조립)와 같은 처리다. "well-known"처럼 원래 하이픈이 있던 단어가 우연히
    줄 끝에서 갈리면 하이픈이 사라지는 오탐이 드물게 있을 수 있지만, 제목
    쪽에서 이미 감수하고 있는 트레이드오프와 같다.
    """
    text = re.sub(r"-\n\s*", "", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    return text.strip()


def _merge_close_text_blocks(entries: list, gap_threshold: float) -> list:
    """세로로 아주 가까운 연속 텍스트 블록을 한 문단으로 합친다.

    수식 기호·각주 표시로 폰트가 바뀌면 PyMuPDF 블록 추출기가 **한 문단을
    여러 블록으로 잘못 쪼갠다**(실측: "in and generates one or more\\noutputs
    x+"처럼 문장 중간이 다음 블록으로 넘어감). 이렇게 쪼개진 블록 사이 간격은
    거의 0(같은 줄이 이어지는 수준)인 반면, 진짜 문단 경계는 이 조판이 문단
    사이에 주는 여백(\\parskip류)만큼 더 떨어져 있다 — 그 차이로 구분한다.
    같은 컬럼(가로 범위가 겹침)일 때만 합쳐서 2단 레이아웃의 다른 컬럼과
    잘못 합쳐지는 걸 막는다.
    """
    merged: list = []
    for kind, b in entries:
        if kind == "text" and merged and merged[-1][0] == "text":
            prev = merged[-1][1]
            gap = b[1] - prev[3]
            same_column = not (b[2] < prev[0] or b[0] > prev[2])
            if same_column and gap < gap_threshold:
                merged[-1] = ("text", (
                    prev[0], prev[1], max(prev[2], b[2]), b[3],
                    prev[4].rstrip("\n") + " " + b[4].lstrip(),
                    prev[5], prev[6]))
                continue
        merged.append((kind, b))
    return merged


def _stitch_split_sentences(paragraphs: list[str]) -> list[str]:
    """페이지/컬럼 경계에서 한 문장이 둘로 쪼개진 걸 다시 잇는다.

    실측: "...This data augmentation strategy extends our"로 페이지가 끝나고,
    다음 페이지가 "dataset from ∼1.6K examples..."로 이어진다 — 같은
    문장인데 문단 두 개로 쪼개져 나온다. _merge_close_text_blocks의 간격
    비교는 **같은 페이지 안에서만** 되므로 이 경우를 못 잡는다.

    문구를 몰라도 되는 일반적인 판단 기준을 쓴다: 앞 문단이 문장부호로 안
    끝나고 뒤 문단이 소문자로 시작하면 원래 한 문장이었던 것으로 본다.

    ⚠️ **"(그림 N)"/"(표 N)" 표시가 사이에 끼어 있으면 합치지 않는다.** 예전엔
    건너뛰고 셋을 한 문자열로 합쳤는데, 그러면 그 그림/표 자리가 앞뒤에 어떤
    문장이 있었는지에 따라 다른 문자열이 된다 — 수정 전/후 버전에서 앞뒤
    문장이 조금만 달라도(다른 데서 생긴 변화 때문에) 같은 그림인데 다른
    문단으로 취급돼 diff가 "그림이 바뀌었다"로 오인하고, 심하면 그림이
    두 번(수정 전 자리·수정 후 자리) 나타나거나 다른 그림이 빠지는 원인이
    됐다(실측). 그림/표는 항상 독립된 문단으로 남겨 문단 매칭이 안정되게
    한다 — 화면에서 문장이 살짝 끊겨 보이는 것보다 그림이 사라지거나
    중복되는 게 더 큰 문제라 이쪽을 택했다.

    ⚠️ 완벽하지 않다 — 목록 항목처럼 원래 문장부호 없이 끝나는 문단이나,
    소문자 약어로 시작하는 새 문단을 잘못 이어붙일 수 있다.
    """
    out: list[str] = []
    i, n = 0, len(paragraphs)
    while i < n:
        cur = paragraphs[i]
        nxt = paragraphs[i + 1] if i + 1 < n else None
        if (nxt is not None and cur and not _MEDIA_PLACEHOLDER_OPENING.match(cur)
                and not _MEDIA_PLACEHOLDER_OPENING.match(nxt)
                and not _SENTENCE_END.search(cur) and _STARTS_LOWER.match(nxt)):
            out.append(cur + " " + nxt)
            i += 2
        else:
            out.append(cur)
            i += 1
    return out


def extract_full_text(pdf_bytes: bytes, max_pages: int = MAX_BODY_PAGES) -> str | None:
    """PDF 전체(제목/초록이 아니라 본문까지)의 텍스트를 뽑는다.

    리비전 본문 diff 전용이다 — 제목/초록만 보는 _page_spans/extract_title_abstract와
    달리 문서 전체를 읽는다. 페이지 수가 max_pages를 넘으면 **잘라서 비교하지 않고
    None을 반환한다**: 양쪽을 max_pages로 독립적으로 절삭하면 실제 수정이 그 뒤
    (예: 부록)에 몰려 있을 때 유사도가 왜곡될 수 있어서다.

    **페이지 통짜 텍스트가 아니라 레이아웃 블록 단위로 모아 "\\n\\n"으로 잇는다.**
    get_text("text")로 페이지 전체를 한 번에 뽑으면 문단 경계가 안 남는다. 다만
    블록 경계를 그대로 믿지는 않는다 — 아래 세 가지를 보정한다:

    1. **문단이 부적절하게 쪼개지는 경우** (_merge_close_text_blocks) — 수식·각주로
       폰트가 바뀌면 한 문단이 여러 블록으로 잘린다. 세로 간격이 좁으면 다시 합친다.
    2. **그림/표 안 텍스트가 본문에 뜬금없이 섞이는 경우** — 그림 영역
       (_figure_regions)에 속한 텍스트는 이미지든 라벨·범례든 전부 버리고
       캡션만 "(그림 N)"으로 남긴다(실측: 그림 하나에 라벨이 6~7개씩 흩어져
       있어도 한 자리로 정리됨). 표는 find_tables()가 찾은 실제 grid
       bbox(_table_regions)를 통째로 지우고 "(표 N)"만 남긴다 — "짧은 블록이면
       표 셀일 것"이라는 word-count 추정만으로는 여러 행이 한 블록에 뭉쳐
       길어지면 못 잡는다(실측: 그렇게 샌 표 데이터가 문장부호 없이 끝나는
       바람에 3번 로직이 다음 문장에 잘못 붙여버렸다).
    3. **페이지·컬럼 경계에서 순수 텍스트 문장이 잘리는 경우**
       (_stitch_split_sentences) — 1번은 같은 페이지 안에서만 작동하므로
       페이지를 넘어가는 절단은 못 잡는다. 문장부호/소문자 시작 여부로
       다시 잇는다. 단, 그림/표 표시가 사이에 끼면 합치지 않는다 — 합치면
       그 표시가 앞뒤 문장에 따라 다른 문자열이 돼 버전 간 diff가 같은
       그림을 다른 것으로 오인한다(중복·누락의 원인이었다, 실측).

    ⚠️ **머리말/꼬리말은 문구가 아니라 "반복되는 정도"로 찾는다.** "Under review
    as a conference paper at ICLR 2025" 같은 문구를 하드코딩하면 다른 학회
    조판엔 안 통한다. 대신 페이지의 30% 이상(최소 3회)에서 토씨 하나 안 틀리고
    반복되는 블록은 저자 본문이 아니라 학회 시스템이 매 페이지 찍는 것으로 본다
    — 이러면 학회·문구에 상관없이 일반적으로 걸러진다.

    ⚠️ **리뷰용 줄번호도 구조로 찾는다.** "under review" 조판(ICLR 등)이 왼쪽
    여백에 박아 넣는 줄번호(000, 001 ...)는 숫자 하나당 블록 하나가 아니라
    여러 개가 한 블록에 줄바꿈으로 뭉쳐 온다(실측: `'000\n001\n'`). 블록 전체가
    숫자 토큰뿐인지로 판단한다.

    fitz.open()을 한 번만 열어 페이지 수 확인과 텍스트 추출을 같은 문서 핸들로
    처리한다 (page_count()를 먼저 호출하고 여기서 또 여는 이중 오픈을 피함).
    """
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if doc.page_count > max_pages:
            return None
        n_pages = doc.page_count
        page_blocks = [_ordered_blocks(doc[i]) for i in range(n_pages)]

        text_counter: dict[str, int] = {}
        for blocks in page_blocks:
            seen = set()
            for b in blocks:
                if b[6] != 0:
                    continue
                t = b[4].strip()
                if t and t not in seen:
                    text_counter[t] = text_counter.get(t, 0) + 1
                    seen.add(t)
        repeating = {t for t, c in text_counter.items()
                    if n_pages >= 3 and c >= max(3, n_pages * 0.3)}

        samples = [h for blocks in page_blocks for h in _line_height_samples(blocks)]
        gap_threshold = (statistics.median(samples) if samples else 12.0) * 0.5

        excluded_rule_ys = _repeating_rule_ys(doc)
        paragraphs: list[str] = []
        for i, blocks in enumerate(page_blocks):
            table_regions = _table_regions(doc[i], excluded_rule_ys)
            table_bottoms = tuple(bottom for _, bottom, _, _, _ in table_regions)
            figure_regions = _figure_regions(doc[i], blocks, table_bottoms)
            box_regions = _box_regions(doc[i], exclude=[
                (top, bottom) for top, bottom, _, _, _ in table_regions + figure_regions])
            equation_regions = _equation_regions(doc[i])
            regions = figure_regions + table_regions + box_regions + equation_regions
            # 캡션은 찾았는데(정규식 매칭) 정작 영역 계산엔 실패하는 경우가
            # 있다(실측: 레이아웃이 특이한 논문에서 종종 발생 — find_tables()가
            # 못 찾고 괘선 기반 fallback도 못 찾는 조합). 이럴 때 그냥 평소처럼
            # "(표 N)" 자리표시자로 바꾸면, extract_media_images엔 그 라벨의
            # 이미지가 없어서 프론트에서 빈 자리로 조용히 사라진다(MediaPiece가
            # media를 못 찾으면 null 렌더) — 표가 통째로 안 보이는데 이유를 알
            # 방법이 없다. 그래서 라벨이 실제로 영역을 확보했는지 미리 표시해
            # 둔다(아래 캡션 처리에서 사용).
            region_labels = {label for _, _, _, _, label in regions}
            # Algorithm 캡션(콜론 없음)은 텍스트만으로 본문 서술과 구분이 안 되므로,
            # _table_regions가 이미 괘선으로 기하학적 확인을 마친 라벨만 캡션으로
            # 인정한다 — "Algorithm 1이 가장 좋다" 같은 본문 문장은 여기 없다.
            confirmed_algo = {label for _, _, _, _, label in table_regions if label.startswith("Algorithm")}
            # find_tables()/괘선으로 grid까지 확인해 이미 통째로 지운 표·알고리즘
            # 라벨. 이 라벨의 본문은 위 regions 겹침 체크(870행)에서 이미 다
            # 걸러지므로, 아래 skip_table_cells 2차 방어선을 또 걸면 안 된다 —
            # 걸면 표 바로 다음에 오는, 우연히 8단어 이하인 무관한 텍스트(실측:
            # 부록 소제목 "A.8\nLIMITATIONS")까지 "새어나온 표 셀"로 오인해
            # 지워버린다.
            grid_detected_labels = {label for _, _, _, _, label in table_regions}

            entries = []
            for b in blocks:
                if b[6] != 0:
                    continue  # 이미지 블록 자체는 여기서 다루지 않는다 — 캡션이 자리를 만든다
                text = b[4].strip()
                if not text:
                    continue
                tokens = text.split()
                if all(_LINE_NUMBER.fullmatch(t) for t in tokens):
                    continue
                if text in repeating:
                    continue

                # ⚠️ **"blocks" 모드가 수식과 바로 다음 문단을 한 블록으로 묶어
                # 돌려줄 때가 있다.** _equation_regions는 줄 단위로 정확한
                # bbox를 찾지만(그래야 이미지가 수식만 딱 잘린다), 이 블록
                # 자체는 여전히 "(1)\n다음 문단 148단어..." 형태로 뭉쳐 있어
                # 완전 포함 조건(아래)에 안 걸리고 통째로 새어 나온다(실측).
                # 이 블록이 수식 영역과 세로로 겹치면, 원본 줄바꿈이 살아있는
                # b[4](flatten 전)에서 "(N)\n" 패턴 뒤쪽만 진짜 다음 문단으로
                # 살리고 앞쪽(수식 자체)은 버린다 — 수식은 이미 별도
                # placeholder로 표시되므로 버려도 데이터가 없어지지 않는다.
                for eq_top, eq_bottom, _eq_x0, _eq_x1, eq_label in equation_regions:
                    if not (b[1] < eq_bottom and b[3] > eq_top):
                        continue
                    num = eq_label.rsplit(" ", 1)[-1]
                    m = re.search(r"\(" + re.escape(num) + r"\)\s*\n", b[4])
                    if m:
                        remainder = b[4][m.end():]
                        b = (b[0], b[1], b[2], b[3], remainder, b[5], b[6])
                        text = remainder.strip()
                        tokens = text.split()
                        break
                if not text:
                    continue

                flat = _flatten_block_text(b[4])
                algo_m = _ALGO_CAPTION.match(flat)
                is_caption = bool(_CAPTION.match(flat)) or bool(
                    algo_m and f"Algorithm {algo_m.group(1)}" in confirmed_algo)
                # ⚠️ **세로 범위만 보면 안 된다.** 그림·표가 페이지 폭
                # 일부만 차지하고 옆 칸에 본문이 계속되는 레이아웃(예: 왼쪽
                # 그림+오른쪽 텍스트)에서, 세로 범위만 겹친다고 제외하면 그
                # 옆 칸의 진짜 본문까지 통째로 사라진다 — 가로 범위도 같이
                # 겹칠 때만(진짜 사각형 포함) 제외한다.
                if not is_caption and any(
                        top - _REGION_CONTAINMENT_TOLERANCE <= b[1]
                        and b[3] <= bottom + _REGION_CONTAINMENT_TOLERANCE
                        and x0 - _REGION_CONTAINMENT_TOLERANCE <= b[0]
                        and b[2] <= x1 + _REGION_CONTAINMENT_TOLERANCE
                        for top, bottom, x0, x1, _ in regions):
                    continue  # 그림/표/알고리즘/박스 영역 안 — 캡션 자신은 항상 통과시킨다
                # 캡션은 별도 kind로 표시해 _merge_close_text_blocks가 앞뒤 문단과
                # 붙이지 않게 한다 — 붙으면 캡션 뒤 진짜 문단까지 "(그림 N)" 한
                # 줄로 통째로 사라진다.
                entries.append(("caption" if is_caption else "text", b))

            # 박스는 원본 PDF에 캡션 블록이 없어(무기명) entries에 자연스럽게
            # 낄 자리가 없다 — y좌표를 담은 가짜 블록으로 만들어 끼워 넣고
            # 위치순으로 다시 정렬한다(Python sort는 안정 정렬이라 같은
            # y좌표의 기존 순서는 그대로 유지된다).
            for top, bottom, _x0, _x1, label in box_regions:
                placeholder = f"({label.replace('Box ', '박스 ')})"
                entries.append(("box", (0.0, top, 0.0, bottom, placeholder, -1, 0)))
            # 수식도 박스처럼 본문에 캡션 블록이 따로 없다(번호만 오른쪽에 찍힘) —
            # 같은 방식으로 가짜 블록에 플레이스홀더를 미리 만들어 끼워 넣는다.
            for top, bottom, _x0, _x1, label in equation_regions:
                placeholder = f"({label.replace('Equation ', '수식 ')})"
                entries.append(("equation", (0.0, top, 0.0, bottom, placeholder, -1, 0)))
            entries.sort(key=lambda kb: kb[1][1])

            skip_table_cells = False
            for kind, b in _merge_close_text_blocks(entries, gap_threshold):
                text = _flatten_block_text(b[4])
                if kind in ("box", "equation"):
                    paragraphs.append(text)  # 이미 "(박스 <해시>)"/"(수식 N)" 형태로 만들어 둠
                    skip_table_cells = False
                    continue
                if kind == "caption":
                    m = _CAPTION.match(text)
                    if m:
                        kind_en, label_word, num = m.group(1).capitalize(), _CAPTION_LABEL_WORD[m.group(1).lower()], m.group(2)
                    else:
                        algo_m = _ALGO_CAPTION.match(text)
                        kind_en, label_word, num = "Algorithm", "알고리즘", algo_m.group(1)
                    if f"{kind_en} {num}" in region_labels:
                        paragraphs.append(f"({label_word} {num})")
                    else:
                        # 영역을 못 찾아 extract_media_images에도 이 라벨의
                        # 이미지가 없다 — 자리표시자로 바꾸면 프론트에서 아무
                        # 표시 없이 사라지므로, 실패했다는 사실 자체를 캡션과
                        # 함께 텍스트로 남긴다(원문은 이 캡션 바로 다음에
                        # 이어지므로 skip_table_cells로 계속 걸러낸다).
                        paragraphs.append(f"({label_word} {num} — 이미지 추출실패, 텍스트로 대체)\n\n{text}")
                    skip_table_cells = (label_word in ("표", "알고리즘")
                                        and f"{kind_en} {num}" not in grid_detected_labels)
                    continue
                if skip_table_cells:
                    # 2차 방어선. find_tables()가 이 표를 못 찾아 _table_regions에
                    # 안 걸렸을 때만 여기까지 온다 — 짧은 블록이면 표 셀로 보고 흡수.
                    if len(text.split()) <= 8:
                        continue
                    skip_table_cells = False
                paragraphs.append(text)

        return "\n\n".join(_stitch_split_sentences(paragraphs))
    finally:
        doc.close()


def extract_media_images(pdf_bytes: bytes, max_pages: int = MAX_BODY_PAGES,
                         dpi: int = 72) -> dict[str, bytes]:
    """PDF에서 그림·표·알고리즘·박스 영역을 전부 찾아 {라벨: PNG 바이트}로 잘라 돌려준다.

    표·알고리즘도 그림과 똑같이 이미지로 비교한다. PyMuPDF의 find_tables()로
    셀 구조까지 뽑아본 적이 있는데, 병합된 셀을 못 풀어내(실측:
    "BlenderAlchemy\nOurs"처럼 방법 이름 여러 개가 한 칸에 뭉침) 원본 표
    모양과 많이 달라졌다 — 텍스트로 재구성하는 것보다 영역을 그대로 이미지로
    잘라 원본과 최대한 비슷하게 보여주는 쪽이 낫다고 판단해 통일했다.

    코드·프롬프트 인용처럼 캡션도 grid도 없는 "박스"(_box_regions)도 같은
    방식으로 잘라 낸다 — 순수 텍스트로 뽑으면 들여쓰기·수식 기호(예:
    "M ←˜ M")가 다 깨져서 원문과 전혀 다른 모양이 된다. 라벨은 저자 번호가
    없어 내용 해시를 쓴다(_box_regions 참고).

    dpi=72: 100dpi 대비 캐시 payload가 거의 절반이면서(실측: 논문 한 편 기준
    1.89MB → 1MB) 텍스트가 있는 다이어그램도 읽을 수 있는 수준은 유지된다.
    before/after 이미지를 트랜지션마다 따로 저장해 중복이 생기지만(같은
    이미지가 앞 리비전의 after와 다음 리비전의 before로 두 번 들어감), 그걸
    없애려면 이미지 풀을 참조하는 별도 스키마가 필요해 지금 범위 밖으로 뺀다.

    그림 하나가 실제로는 이미지 여러 장 + 라벨 텍스트 + 캡션으로 흩어져 있는
    경우가 많아(실측: Figure 1이 이미지 5개 조각), 개별 조각을 따로 자르지
    않는다. 대신 **"Figure N" 캡션 바로 앞의 마지막 '진짜 본문 문단'(20단어
    초과) 끝부터 캡션 끝까지**(_figure_regions)를 그림 전체 영역으로, **find_tables()나
    괘선으로 찾은 bbox**(_table_regions)를 표·알고리즘 영역으로, **배경색
    사각형의 bbox**(_box_regions)를 박스 영역으로 보고 페이지 폭 전체를 그
    높이만큼 크롭한다 — extract_full_text가 같은 영역을 본문에서 지우는
    것과 동일한 판단 기준을 쓴다. 영역 여러 개가 사이에 본문 없이 연달아
    나오면 앞 영역과 겹칠 수 있다(드문 경우로 보고 감수한다).
    """
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if doc.page_count > max_pages:
            return {}
        out: dict[str, bytes] = {}
        excluded_rule_ys = _repeating_rule_ys(doc)
        for pno in range(doc.page_count):
            page = doc[pno]
            ordered_blocks = _ordered_blocks(page)
            table_regions = _table_regions(page, excluded_rule_ys)
            table_bottoms = tuple(bottom for _, bottom, _, _, _ in table_regions)
            figure_regions = _figure_regions(page, ordered_blocks, table_bottoms)
            box_regions = _box_regions(page, exclude=[
                (top, bottom) for top, bottom, _, _, _ in table_regions + figure_regions])
            equation_regions = _equation_regions(page)
            page_rect = page.rect
            # 페이지 높이에 상대적인 상한을 쓴다 — 고정값(예: 600pt)은 페이지
            # 대부분을 차지하는 큰 그림(실측: region_top이 페이지 맨 위인
            # 정당한 경우, 높이 700pt)까지 오탐으로 잘라버린다.
            max_height = page_rect.height * 0.9
            for top, bottom, x0, x1, label in (table_regions + figure_regions + box_regions
                                                + equation_regions):
                if (x1 - x0) >= page_rect.width * _FULL_WIDTH_REGION_RATIO:
                    # 페이지 폭 대부분을 차지하는 보통 경우 — 기존 그대로
                    # 페이지 여백만 20pt 남기고 전체 폭을 쓴다.
                    bbox = fitz.Rect(page_rect.x0 + 20, max(top, 0),
                                     page_rect.x1 - 20, bottom + 5)
                else:
                    # 그림·표가 페이지 폭 일부만 차지하고 옆 칸에 본문이
                    # 이어지는 레이아웃 — 실제 내용 폭만큼만 크롭해서 옆 칸
                    # 텍스트가 이미지에 같이 찍히지 않게 한다. 좌우 여백은
                    # 기본 10pt를 두되, 그 여백 안에(옆 칸 본문처럼 세로로
                    # 겹치면서 x범위는 이 영역 밖인) 진짜 텍스트 블록이 있으면
                    # 그 블록 바로 앞에서 멈춘다 — 안 그러면 좁은 여백 안으로
                    # 옆 칸 글자 몇 개가 삐져 들어온다(실측: 우측 칸 텍스트의
                    # 첫 몇 글자가 크롭 오른쪽 끝에 잘려서 찍힘).
                    # 옆 블록이 이 영역 경계에 딱 붙어 있지 않고 걸쳐
                    # 있을(예: 좁은 캡션 옆에서 재개된 전체 폭 문단처럼
                    # x0는 이 영역 안, x1은 밖으로 걸침) 수도 있으므로,
                    # "옆에서 시작하는" 블록뿐 아니라 "이 영역 밖으로 걸쳐
                    # 넘어가는" 블록도 같이 본다 — 두 경우 다 여백을 그
                    # 블록 쪽 경계까지로 줄인다(마진 없이 딱 붙임).
                    pad_x0, pad_x1 = x0 - 10, x1 + 10
                    for ob in ordered_blocks:
                        if ob[6] != 0 or ob[1] >= bottom or ob[3] <= top:
                            continue
                        if ob[0] >= x0 and ob[0] < x1 and ob[2] <= x1:
                            continue  # 이 영역 자신의 내용(캡션 등) — 무시
                        if ob[0] >= x1 and ob[0] < pad_x1:
                            pad_x1 = ob[0] - 2
                        elif ob[2] > x1 and ob[0] < pad_x1:
                            pad_x1 = min(pad_x1, x1)
                        if ob[2] <= x0 and ob[2] > pad_x0:
                            pad_x0 = ob[2] + 2
                        elif ob[0] < x0 and ob[2] > pad_x0:
                            pad_x0 = max(pad_x0, x0)
                    bbox = fitz.Rect(max(page_rect.x0, pad_x0), max(top, 0),
                                     min(page_rect.x1, pad_x1), bottom + 5)
                if bbox.height < _MIN_CROP_HEIGHT or bbox.height > max_height:
                    continue  # 말이 안 되는 크기는 오탐으로 보고 버린다
                pix = page.get_pixmap(clip=bbox, dpi=dpi)
                out[label] = pix.tobytes("png")
        return out
    finally:
        doc.close()


_SIGNATURE_SIZE = (24, 18)


def image_signature(png_bytes: bytes, size: tuple[int, int] = _SIGNATURE_SIZE) -> bytes:
    """PNG 이미지를 아주 작은 고정 격자(그레이스케일)로 리샘플해 시각적
    "특징"만 남긴다. Figure/Table/Algorithm은 저자 번호로 짝짓지만, 저자가
    문서 안에서 번호를 재배치하면(실측: Figure 5/6 순서가 바뀌어 완전히
    다른 그림이 같은 번호를 갖게 됨) 번호만 믿는 매칭이 엉뚱한 두 그림을
    "수정 전/후"로 짝짓는다 — attach_body_diffs가 이 시그니처로 "번호는
    같은데 내용이 많이 다른지"를 먼저 확인하고, 다르면 번호가 바뀐 다른
    그림들 중에서 진짜 짝을 찾는다.

    원본 종횡비·해상도와 무관하게 고정 크기로 리샘플하므로 크기가 다른 두
    이미지도 바로 비교할 수 있고, 실측으로 완전히 동일한 이미지는 항상
    유사도 1.0, 명백히 다른 이미지는 0.8~0.9대로 뚜렷하게 갈렸다(표1 참고
    — attach_body_diffs의 _FIGURE_MATCH_THRESHOLD 산정 근거).
    """
    import fitz  # PyMuPDF

    if not png_bytes:
        return b""
    w, h = size
    pix = fitz.Pixmap(png_bytes)
    pix = fitz.Pixmap(pix, w, h)
    if pix.colorspace is None or pix.colorspace.name != "DeviceGray":
        pix = fitz.Pixmap(fitz.csGRAY, pix)
    return pix.samples


def image_similarity(a: bytes, b: bytes) -> float:
    """image_signature 결과 두 개를 0(완전히 다름)~1(완전히 동일)로 비교한다."""
    if not a or not b or len(a) != len(b):
        return 0.0
    diff = sum(abs(x - y) for x, y in zip(a, b))
    return 1 - diff / (len(a) * 255)


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
