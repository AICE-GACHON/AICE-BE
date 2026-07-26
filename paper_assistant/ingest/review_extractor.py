"""리뷰 → 지적 항목 추출.

두 가지 구현을 같은 인터페이스(PointExtractor)로 제공한다:

  HeuristicExtractor : LLM 없이 규칙 기반. 비용 $0. (현재 MVP 기본값)
  HaikuExtractor     : Claude Haiku로 추출. 품질은 높지만 유료. (플레이스홀더)

둘 다 ExtractedPoint 리스트를 반환하므로, 품질이 부족하면 수집 스크립트에서
extractor만 바꿔 끼우면 된다 — 다운스트림(임베딩·클러스터링·집계)은 그대로.

Heuristic 방식의 근거:
리뷰어는 약점을 대부분 불릿(-, *, •), 번호(1. 2.), 또는 문장 단위로 나열한다.
이 구조를 이용해 지적 항목을 분리하고, 통제된 aspect 분류(§4)는 키워드 매칭으로
근사한다. LLM만큼 정교하진 않지만 클러스터링의 1차 그룹핑에는 충분하다.
"""
import re
from dataclasses import dataclass
from typing import Protocol

from paper_assistant.ingest.normalize import NormalizedReview

# 통제된 aspect 분류 — normalize.py / init_db.sql 과 동일한 9개 체계.
# 각 aspect의 대표 키워드(소문자, 정규식). 우선순위는 리스트 순서.
ASPECT_KEYWORDS: list[tuple[str, list[str]]] = [
    ("experimental_scale", [
        r"\bimagenet\b", r"\blarger (?:dataset|scale|benchmark)", r"\bsmall(?:er)? (?:dataset|scale)",
        r"\btoy (?:dataset|example)", r"\bonly .{0,20}(?:cifar|mnist)", r"\bscalab", r"\bscale up\b"]),
    ("baselines", [
        r"\bbaseline", r"\bcompar(?:e|ison|ed) (?:to|with|against)", r"\bsota\b",
        r"\bstate[- ]of[- ]the[- ]art", r"\bmissing compar", r"\bstronger baseline"]),
    ("novelty", [
        r"\bnovelt", r"\bincrement", r"\bnot novel", r"\blimited (?:novelty|contribution)",
        r"\bmarginal (?:contribution|improvement)", r"\bsimilar to (?:prior|existing)"]),
    ("theoretical_soundness", [
        r"\btheor", r"\bproof\b", r"\bassumption", r"\bderivation", r"\bmathematic",
        r"\bformal(?:ly)? (?:incorrect|wrong)", r"\brigor"]),
    ("reproducibility", [
        r"\breproduc", r"\bcode (?:is )?(?:not )?(?:available|released)", r"\bimplementation detail",
        r"\bhyperparameter", r"\bunclear how to"]),
    ("clarity", [
        r"\bunclear", r"\bhard to (?:follow|read|understand)", r"\bconfusing", r"\bpoorly written",
        r"\bnotation", r"\btypo", r"\bpresentation (?:is|could)", r"\bwriting"]),
    ("related_work", [
        r"\brelated work", r"\bmissing (?:citation|reference)", r"\bprior work",
        r"\bfails? to cite", r"\bliterature"]),
    ("significance", [
        r"\bsignifican", r"\bimpact", r"\bpractical (?:use|value|relevance)", r"\bmotivat",
        r"\bwhy .{0,30}(?:matter|important)", r"\bnot useful"]),
]

# 지적 항목 분리용 패턴
_BULLET = re.compile(r"^\s*(?:[-*•·▪]|\(?\d+[.)]|\(?[a-z][.)])\s+", re.MULTILINE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
MIN_POINT_CHARS = 25   # 이보다 짧으면 지적 항목으로 보지 않는다
MAX_POINTS_PER_REVIEW = 12

# --- 미분리 리뷰의 강/약점 섹션 복구 -------------------------------------
# 2023년 이전 venue는 강/약점 필드가 없어 리뷰 본문 전체가 weaknesses로 들어온다.
# 그 본문을 통째로 '지적'으로 라벨링하면 강점·요약 문장까지 weakness가 되어
# 논문당 지적 수가 1.47배(27.9 vs 18.9건)로 부풀고 aspect 지적률도 최대 1.39배
# 왜곡된다. 다만 실측 결과 미분리 리뷰 62,346건 중 **46.0%**는 'Cons' /
# 'Weaknesses' / 'Concerns' 같은 머리말을 그대로 갖고 있어 정규식으로 되살릴 수
# 있다. 나머지(머리말 없는 산문형)는 sentiment='unknown'으로 빼서 집계에서 제외한다.
_WEAK_HEAD = re.compile(
    r"^[ \t]*(?:\*{0,2}|#{0,3})[ \t]*(?:cons|weakness(?:es)?|limitations?|"
    r"drawbacks?|concerns?|criticisms?|negatives?|issues?)\b[ \t]*:?[ \t]*",
    re.IGNORECASE | re.MULTILINE)
_STRONG_HEAD = re.compile(
    r"^[ \t]*(?:\*{0,2}|#{0,3})[ \t]*(?:pros|strengths?|positives?|"
    r"advantages?|summary|questions?)\b[ \t]*:?[ \t]*",
    re.IGNORECASE | re.MULTILINE)
# 잘라낸 섹션이 이보다 짧으면 머리말만 있고 내용이 없는 것으로 본다
MIN_SECTION_CHARS = 40


def split_weakness_section(text: str) -> str | None:
    """미분리 리뷰 본문에서 약점 섹션만 잘라낸다. 머리말이 없으면 None.

    약점 머리말부터 '다음 머리말(강점이든 약점이든)' 직전까지를 한 구간으로 보고,
    여러 구간이 있으면 모두 이어 붙인다. 'Pros ... Cons ... Questions' 처럼
    섹션이 이어지는 형식을 그대로 따라간다.
    """
    if not text:
        return None

    # (위치, 머리말 끝, 약점인가) 목록을 만들어 위치순으로 훑는다
    heads = [(m.start(), m.end(), True) for m in _WEAK_HEAD.finditer(text)]
    heads += [(m.start(), m.end(), False) for m in _STRONG_HEAD.finditer(text)]
    if not any(is_weak for _, _, is_weak in heads):
        return None
    heads.sort()

    sections = []
    for i, (_start, head_end, is_weak) in enumerate(heads):
        if not is_weak:
            continue
        next_start = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        body = text[head_end:next_start].strip()
        if len(body) >= MIN_SECTION_CHARS:
            sections.append(body)

    if not sections:
        return None
    return "\n".join(sections)


@dataclass
class ExtractedPoint:
    aspect: str
    sentiment: str   # weakness / strength / question
    text: str


class PointExtractor(Protocol):
    def extract(self, review: NormalizedReview) -> list[ExtractedPoint]: ...


def classify_aspect(text: str) -> str:
    """키워드 매칭으로 aspect를 근사. 매칭 없으면 'other'."""
    low = text.lower()
    for aspect, patterns in ASPECT_KEYWORDS:
        if any(re.search(p, low) for p in patterns):
            return aspect
    return "other"


def _split_points(text: str) -> list[str]:
    """텍스트를 지적 항목 단위로 분리.

    불릿/번호 목록이 있으면 그 단위로, 없으면 문장 단위로 쪼갠다.
    """
    text = text.strip()
    if not text:
        return []

    # 불릿/번호가 2개 이상이면 목록으로 간주하고 그 경계로 분리
    if len(_BULLET.findall(text)) >= 2:
        parts = _BULLET.split(text)
    else:
        parts = _SENTENCE_SPLIT.split(text)

    points = [p.strip() for p in parts if len(p.strip()) >= MIN_POINT_CHARS]
    return points[:MAX_POINTS_PER_REVIEW]


class HeuristicExtractor:
    """LLM 없이 규칙 기반으로 지적 항목을 추출. 비용 $0."""

    def extract(self, review: NormalizedReview) -> list[ExtractedPoint]:
        points: list[ExtractedPoint] = []

        # 강/약이 분리된 venue는 weaknesses 필드가 곧 약점 목록이라 그대로 쓴다.
        # 미분리 venue는 본문 전체가 들어오므로 (1) 약점 섹션을 정규식으로 되살려
        # 보고, (2) 실패하면 weakness라 단정하지 않고 'unknown'으로 남긴다.
        # 예전에는 본문 전체를 weakness로 넣었는데, 그러면 강점·요약 문장까지
        # 지적으로 집계되어 지적률이 계통적으로 부풀었다.
        body, sentiment = review.weaknesses, "weakness"
        if review.needs_llm_split:
            recovered = split_weakness_section(review.weaknesses)
            if recovered is not None:
                body = recovered
            else:
                sentiment = "unknown"

        for chunk in _split_points(body):
            points.append(ExtractedPoint(
                aspect=classify_aspect(chunk),
                sentiment=sentiment,
                text=chunk,
            ))

        # 질문도 사실상 지적인 경우가 많아 포함 (sentiment로 구분)
        for chunk in _split_points(review.questions):
            points.append(ExtractedPoint(
                aspect=classify_aspect(chunk),
                sentiment="question",
                text=chunk,
            ))
        return points[:MAX_POINTS_PER_REVIEW]


class HaikuExtractor:
    """Claude Haiku 기반 추출 (플레이스홀더).

    품질이 부족하다고 판단되면 이 클래스를 구현해 HeuristicExtractor 대신 끼운다.
    수집 단계에서 Batch API로 돌리는 것을 권장 (비용 50% 절감).
    구현 시 통제된 aspect 분류를 프롬프트에 명시하고 ExtractedPoint를 반환할 것.
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "HaikuExtractor는 아직 미구현. 예산 확보 후 구현하세요 "
            "(AI_파트_설계서.md §4, §13 참고). 현재는 HeuristicExtractor를 쓰세요.")

    def extract(self, review: NormalizedReview) -> list[ExtractedPoint]:
        raise NotImplementedError
