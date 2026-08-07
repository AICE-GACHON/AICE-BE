"""근거 풀 조립 + 인용 표기 검증 (RAG 근거 추적).

생성 단계가 실제로 **선정된 5편의 원문**에 기반하도록, 그 논문들의 리뷰 지적 문장과
메타리뷰를 라벨(E1, M1 …)이 붙은 근거 풀로 만들어 프롬프트에 넣는다. 그리고 모델이
쓴 `[E1]` 표기를 풀과 대조해 **없는 라벨은 지운다**.

검증이 없으면 "인용해 달라"는 부탁일 뿐이다. 모델이 `[E9]`를 지어내도 화면에는
근거가 달린 것처럼 보인다.

⚠️ **검증의 범위**: 라벨이 실재하는지만 본다. 그 문장이 원문에서 나온 내용인지는
검사하지 않는다 — 실재하는 라벨을 엉뚱한 문장에 붙이면 그대로 통과한다(실측:
experimental_scale 비판을 인용하며 "강점으로 꼽힌다"고 쓴 사례). 따라서 보장은
"모든 인용은 실제 원문으로 **역추적된다**"까지이고, "그 원문이 그 주장을
**뒷받침한다**"는 아니다. 후자는 프롬프트로 유도할 뿐 강제하지 못한다.

DB도 LLM도 모르는 순수 함수라 테스트가 쉽다.
"""
import re

from paper_assistant.schemas import EvidenceItem

# 근거로 넣을 최대 개수. 프롬프트가 길어지면 모델이 요약 대신 문장만 베낀다.
MAX_POINTS_PER_PAPER = 2
MAX_META_REVIEWS = 3
MAX_TEXT = 400          # 지적 문장 1건
MAX_META_TEXT = 700     # 메타리뷰 1건 (AC 총평이라 길다)

# 인용 표기: 라벨만 들어 있는 대괄호 블록만 건드린다.
# 마크다운 링크 [텍스트](url)를 망가뜨리지 않도록 패턴을 좁게 잡는다.
_CITE_BLOCK = re.compile(r"\[((?:[EM]\d+)(?:\s*,\s*[EM]\d+)*)\]")


def build_evidence_for_selected(selected, points_by_paper: dict[int, list[dict]],
                                max_per_paper: int = MAX_POINTS_PER_PAPER,
                                max_meta: int = MAX_META_REVIEWS
                                ) -> list[EvidenceItem]:
    """**선정된 5편**의 리뷰 원문으로 근거 풀을 만든다.

    예전에는 aspect 집계(ReviewPattern)의 대표 문장에서 뽑았다. 그때는 이웃 20편의
    통계가 결론이었기 때문이다. 지금 결론은 "이 5편이 이런 지적을 받았다"이므로,
    근거도 **그 5편에서 직접** 나와야 한다 — 집계에서 뽑으면 화면에 없는 논문의
    문장이 근거로 붙을 수 있다.

    E* = 지적 문장, M* = AC 총평. 라벨은 여기서 부여하고 프롬프트와 응답 검증이
    같은 라벨을 쓴다 (validate_citations).
    """
    evidence: list[EvidenceItem] = []
    for paper in selected:
        for point in points_by_paper.get(paper.paper_id, [])[:max_per_paper]:
            evidence.append(EvidenceItem(
                label=f"E{len(evidence) + 1}",
                kind="review_point",
                text=point["text"][:MAX_TEXT],
                paper_id=paper.paper_id,
                paper_title=paper.title,
                review_point_id=point.get("point_id"),
                aspect=point.get("aspect"),
                from_unsplit_review=bool(point.get("from_unsplit")),
            ))

    n_meta = 0
    for paper in selected:
        if n_meta >= max_meta:
            break
        text = (paper.meta_review or "").strip()
        if not text:
            continue
        n_meta += 1
        evidence.append(EvidenceItem(
            label=f"M{n_meta}", kind="meta_review", text=text[:MAX_META_TEXT],
            paper_id=paper.paper_id, paper_title=paper.title,
            decision=paper.decision))
    return evidence



def format_for_prompt(evidence: list[EvidenceItem]) -> list[dict]:
    """LLM에 넘길 형태. 라벨을 맨 앞에 둬서 모델이 참조하기 쉽게 한다."""
    out = []
    for e in evidence:
        item = {"label": e.label, "kind": e.kind,
                "paper": e.paper_title[:120], "text": e.text}
        if e.kind == "meta_review" and e.decision:
            item["decision"] = e.decision
        elif e.aspect:
            item["aspect"] = e.aspect
        out.append(item)
    return out


def validate_citations(text: str,
                       evidence: list[EvidenceItem]) -> tuple[str, list[str]]:
    """요약문의 인용 표기를 근거 풀과 대조한다.

    풀에 없는 라벨은 지운다(모델이 지어낸 것). 라벨이 하나도 안 남은 블록은
    대괄호째 제거한다. 반환: (정리된 텍스트, 실제로 인용된 라벨 목록).
    """
    known = {e.label for e in evidence}
    used: list[str] = []

    def _fix(match: re.Match) -> str:
        labels = [x.strip() for x in match.group(1).split(",")]
        kept = [x for x in labels if x in known]
        for x in kept:
            if x not in used:
                used.append(x)
        return f"[{', '.join(kept)}]" if kept else ""

    cleaned = _CITE_BLOCK.sub(_fix, text)
    # 라벨을 지우면서 생긴 이중 공백 정리 (줄바꿈은 보존)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([,.)])", r"\1", cleaned)
    return cleaned, used
