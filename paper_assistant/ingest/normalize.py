"""venue×연도별로 제각각인 OpenReview 필드를 단일 스키마로 흡수한다.

실측 기준 (scripts/probe_venues.py, probe_v1.py 결과):

  venue          리뷰 본문 필드                    점수 필드        강/약 분리
  ICLR 2020      review                            rating          없음(통짜)
  ICLR 2021      review                            rating          없음(통짜)
  ICLR 2022      main_review                       recommendation  없음(통짜)
  ICLR 2023      strength_and_weaknesses           recommendation  합쳐짐
  ICLR 2024/25   strengths + weaknesses            rating          분리됨
  NeurIPS 2021   main_review                       rating          없음(통짜)
  NeurIPS 2022   strengths_and_weaknesses          rating          합쳐짐
  NeurIPS 23/24  strengths + weaknesses            rating          분리됨

강/약이 분리된 venue는 weaknesses 필드만 LLM에 넘기면 되고, 그렇지 않은 venue는
본문 전체를 넘겨야 한다 → `needs_llm_split`으로 구분해 토큰 비용을 줄인다.
"""
import re
from dataclasses import dataclass, field

# 리뷰 본문 후보 필드 (우선순위 순). 앞쪽이 더 구조화된 형태.
WEAKNESS_FIELDS = ["weaknesses"]
STRENGTH_FIELDS = ["strengths"]
COMBINED_FIELDS = ["strength_and_weaknesses", "strengths_and_weaknesses"]
FULLTEXT_FIELDS = ["main_review", "review"]
SUMMARY_FIELDS = ["summary", "summary_of_the_paper"]
QUESTION_FIELDS = ["questions"]
RATING_FIELDS = ["rating", "recommendation"]
# 메타리뷰 본문. Meta_Review 노트가 없는 venue는 Decision 노트에서 같은 이름으로 찾는다.
METAREVIEW_FIELDS = ["metareview", "meta_review",
                     "metareview:_summary,_strengths_and_weaknesses", "comment"]

# venue 문자열 → 정규화된 decision
_ACCEPT_PATTERNS = [
    (r"\boral\b", "accept-oral"),
    (r"\bspotlight\b", "accept-spotlight"),
    (r"\bposter\b", "accept-poster"),
    (r"\bnotable\b", "accept-notable"),
    (r"\baccept", "accept"),
]


def clean_text(s: str) -> str:
    """NUL(0x00) 및 기타 제어 문자 제거.

    일부 논문 초록·리뷰에 NUL 바이트가 섞여 있어 Postgres text 컬럼이 거부한다
    (실측: ICLR 2021). 탭·개행은 보존하고 나머지 제어 문자만 제거한다.
    """
    if not s:
        return s
    return "".join(c for c in s if c == "\t" or c == "\n" or ord(c) >= 0x20)


def unwrap(value):
    """API v2는 모든 content 필드를 {'value': x}로 감싼다. v1은 raw."""
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def get_field(content: dict, names: list[str]) -> str:
    for name in names:
        if name in content:
            val = unwrap(content[name])
            if isinstance(val, str) and val.strip():
                return clean_text(val.strip())
    return ""


def parse_score(raw) -> float | None:
    """'8: Accept' -> 8.0, '5' -> 5.0, '2 fair' -> 2.0, 숫자면 그대로."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    m = re.match(r"\s*(\d+(?:\.\d+)?)", str(raw))
    return float(m.group(1)) if m else None


def normalize_decision(venue_str: str, decision_str: str = "") -> str:
    """게재 결과를 정규화. venue 문자열 우선, 없으면 Decision 노트 값 사용.

    ICLR 2020/2021은 submission content에 venue 필드가 없어 decision_str이 필수.
    """
    text = (venue_str or "").lower()
    if "withdraw" in text:
        return "withdrawn"
    if "desk reject" in text or "desk_reject" in text:
        return "desk-reject"
    for pattern, label in _ACCEPT_PATTERNS:
        if re.search(pattern, text):
            return label
    if text.startswith("submitted to"):
        return "reject"

    # venue 문자열로 판별 불가 → Decision 노트 fallback
    d = (decision_str or "").lower()
    if not d:
        return "unknown"
    if "withdraw" in d:
        return "withdrawn"
    if "reject" in d:
        return "reject"
    for pattern, label in _ACCEPT_PATTERNS:
        if re.search(pattern, d):
            return label
    return "unknown"


@dataclass
class NormalizedReview:
    openreview_id: str
    rating: float | None
    rating_raw: str
    confidence: float | None
    summary: str
    strengths: str
    weaknesses: str
    questions: str
    needs_llm_split: bool
    """True면 강점/약점이 분리되지 않아 LLM에 본문 전체를 넘겨야 한다."""

    @property
    def llm_input(self) -> str:
        """지적 항목 추출 시 LLM에 넘길 텍스트 (토큰 최소화)."""
        return self.weaknesses or self.summary


@dataclass
class NormalizedPaper:
    openreview_id: str
    forum_id: str
    title: str
    abstract: str
    keywords: list[str]
    authors: list[str]
    author_ids: list[str]
    venue: str
    year: int
    decision: str
    primary_area: str
    reviews: list[NormalizedReview] = field(default_factory=list)
    meta_review: str = ""


def normalize_review(note: dict) -> NormalizedReview:
    c = note.get("content", {})

    strengths = get_field(c, STRENGTH_FIELDS)
    weaknesses = get_field(c, WEAKNESS_FIELDS)
    needs_split = False

    if not weaknesses:
        # 강/약 합침 필드 → 그대로 두고 LLM이 쪼개게 한다
        combined = get_field(c, COMBINED_FIELDS)
        if combined:
            weaknesses = combined
            needs_split = True
        else:
            # 통짜 리뷰 본문
            weaknesses = get_field(c, FULLTEXT_FIELDS)
            needs_split = bool(weaknesses)

    rating_raw = ""
    for f in RATING_FIELDS:
        if f in c:
            rating_raw = clean_text(str(unwrap(c[f])))
            break

    return NormalizedReview(
        openreview_id=note.get("id", ""),
        rating=parse_score(rating_raw),
        rating_raw=rating_raw,
        confidence=parse_score(unwrap(c.get("confidence"))),
        summary=get_field(c, SUMMARY_FIELDS),
        strengths=strengths,
        weaknesses=weaknesses,
        questions=get_field(c, QUESTION_FIELDS),
        needs_llm_split=needs_split,
    )


def _invitation_kinds(note: dict) -> list[str]:
    """v2는 invitations(복수), v1은 invitation(단수)."""
    invs = note.get("invitations") or ([note["invitation"]] if note.get("invitation") else [])
    return [i.split("/")[-1] for i in invs]


def normalize_paper(submission: dict, replies: list[dict],
                    venue_name: str, year: int) -> NormalizedPaper:
    c = submission.get("content", {})

    reviews, meta_review, decision_meta, decision_str = [], "", "", ""
    for note in replies:
        kinds = _invitation_kinds(note)
        if any(k == "Official_Review" for k in kinds):
            reviews.append(normalize_review(note))
        elif any(k == "Meta_Review" for k in kinds):
            meta_review = get_field(note.get("content", {}), METAREVIEW_FIELDS)
        elif any(k == "Decision" for k in kinds):
            content = note.get("content", {})
            decision_str = get_field(content, ["decision", "recommendation"])
            # Meta_Review 노트가 없는 venue(ICLR 2020~2023, NeurIPS 2021/23/24)는
            # 메타리뷰 본문이 Decision 노트 안에 들어있다.
            decision_meta = get_field(content, METAREVIEW_FIELDS)

    meta_review = meta_review or decision_meta

    def _clean_list(v):
        if not isinstance(v, list):
            return []
        return [clean_text(x) if isinstance(x, str) else x for x in v]

    keywords = _clean_list(unwrap(c.get("keywords")) or [])
    authors = _clean_list(unwrap(c.get("authors")) or [])
    author_ids = _clean_list(unwrap(c.get("authorids")) or [])

    return NormalizedPaper(
        openreview_id=submission.get("id", ""),
        forum_id=submission.get("forum", ""),
        title=get_field(c, ["title"]),
        abstract=get_field(c, ["abstract"]),
        keywords=keywords,
        authors=authors,
        author_ids=author_ids,
        venue=venue_name,
        year=year,
        decision=normalize_decision(get_field(c, ["venue"]), decision_str),
        primary_area=get_field(c, ["primary_area",
                                   "Please_choose_the_closest_area_that_your_submission_falls_into"]),
        reviews=reviews,
        meta_review=meta_review,
    )
