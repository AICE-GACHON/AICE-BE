"""리뷰 지적항목 → 패턴 집계.

**설계 변경 근거 1 (§14 실측):**
SPECTER2로 지적항목 문장을 임베딩하면 유사도가 평균 0.872에 압축되어(논문
title+abstract용 모델이라 짧은 리뷰 문장을 변별 못 함), 임계값 0.80에서도 대부분
한 클러스터로 뭉친다. 따라서 임베딩 클러스터링 대신 **키워드 aspect 기반 집계**를
1차 방법으로 쓴다. 쿼리 시점 임베딩 불필요.

**설계 변경 근거 2 (§18 실측) — 빈도순 정렬 폐기:**
aspect 빈도를 그냥 세면 코퍼스 base rate가 높은 aspect가 항상 1등이 된다.
실측 base rate는 baselines 78.8% / clarity 65.8% / significance 63.6%로,
"20편 중 17편이 baselines 지적"은 lift 1.08 — 정보량이 사실상 0이다.
그래서 **lift(관측률 ÷ base rate) + 이항검정**으로 재정렬하고, 여기에
**당락 대조**(이 지적을 받은 이웃 vs 아닌 이웃의 accept율)를 붙인다.
후자가 사용자가 실제로 쓰는 정보다 — "이 지적은 받아도 붙는다 / 받으면 떨어진다".

_greedy_cluster는 범용 유틸로 남겨둔다 (향후 aspect 내부 세분화 등에 사용 가능).
"""
from math import comb

import numpy as np

from paper_assistant.schemas import ReviewPattern

# aspect 표시 라벨 (프론트/요약용)
ASPECT_LABELS = {
    "experimental_scale": "실험 규모·범위",
    "baselines": "베이스라인 비교 부족",
    "novelty": "신규성 부족",
    "theoretical_soundness": "이론적 근거",
    "reproducibility": "재현성",
    "clarity": "명확성·서술",
    "related_work": "관련 연구 누락",
    "significance": "중요성·동기",
    "other": "기타 지적",
}


# 두드러진 지적으로 인정할 기준. lift만 보면 n이 작을 때 노이즈가 올라오므로
# 이항검정 p값을 함께 건다 (n=20에서 lift 1.3은 흔한 우연이다).
MIN_LIFT = 1.25
MAX_P_VALUE = 0.05


def binom_tail(k: int, n: int, p: float, upper: bool = True) -> float:
    """X~Bin(n,p)의 꼬리확률. upper면 P(X>=k), 아니면 P(X<=k).

    이웃 수 n이 20~50이라 정확 계산이 충분히 빠르다 (scipy 의존성 회피).
    """
    if n <= 0 or not 0.0 < p < 1.0:
        return 1.0
    k = max(0, min(k, n))
    rng = range(k, n + 1) if upper else range(0, k + 1)
    total = sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in rng)
    return min(1.0, max(0.0, total))


def fisher_exact_less(a: int, b: int, c: int, d: int) -> float:
    """2x2 분할표의 단측 Fisher 정확검정 — a가 우연보다 작을 확률.

        [[a, b],   a=지적받고 accept, b=지적받고 reject
         [c, d]]   c=미지적 accept,   d=미지적 reject

    당락 대조는 표본이 매우 작다(이웃 20편 중 4편이 지적받는 식). 검정 없이
    "지적받은 4편 중 0편 통과"를 결론으로 내면 노이즈를 사실로 파는 셈이다.
    카이제곱은 기대빈도 5 미만에서 못 쓰므로 정확검정을 쓴다.
    """
    n = a + b + c + d
    if n == 0 or (a + b) == 0 or (c + d) == 0 or (a + c) == 0 or (b + d) == 0:
        return 1.0
    row1, col1 = a + b, a + c
    denom = comb(n, col1)
    total = sum(comb(row1, i) * comb(n - row1, col1 - i)
                for i in range(0, a + 1)
                if 0 <= col1 - i <= n - row1)
    return min(1.0, max(0.0, total / denom))


def _accept_contrast(with_ids: set, all_ids: set,
                     decisions: dict) -> tuple[int, int, int, int]:
    """(accept_with, decided_with, accept_without, decided_without).

    결과가 'unknown'인 논문은 분모에서 뺀다. withdrawn은 '통과하지 못함'으로
    센다 — 리뷰가 나쁘게 나온 뒤 철회한 경우가 대부분이라 reject와 같은 신호다.
    """
    aw = dw = ao = do = 0
    for pid in all_ids:
        decision = decisions.get(pid)
        if not decision or decision == "unknown":
            continue
        accepted = decision.startswith("accept")
        if pid in with_ids:
            dw += 1
            aw += accepted
        else:
            do += 1
            ao += accepted
    return aw, dw, ao, do


def aggregate_by_aspect(points: list[dict], total_papers: int,
                        base_rates: dict[str, float] | None = None,
                        decisions: dict[int, str] | None = None,
                        min_papers: int = 2, top_k: int = 8,
                        include_other: bool = False,
                        all_paper_ids: set | None = None) -> list[ReviewPattern]:
    """지적항목을 aspect별로 묶어 패턴 생성 + base rate 대비 lift·당락 대조.

    points: [{"paper_id", "aspect", "text"}, ...] (weakness sentiment 권장)
    total_papers: 분석 대상 유사 논문 총수 (분모)
    base_rates: {aspect: 코퍼스 전체 지적률(0~1)}. 없으면 lift 계산을 건너뛰고
        예전처럼 빈도순으로 정렬한다 (DB 없는 테스트/폴백 경로).
    decisions: {paper_id: decision}. 없으면 당락 대조를 건너뛴다.
    all_paper_ids: 이웃 전체 id. 지적이 하나도 없는 논문까지 분모에 넣기 위해 필요.
        생략하면 points에 등장한 논문만으로 대조군을 만든다.
    include_other: 'other' 버킷도 패턴으로 낼지 (기본 제외 — 일회성 지적이 대부분)
    """
    base_rates = base_rates or {}
    decisions = decisions or {}

    by_aspect: dict[str, list[dict]] = {}
    seen_ids: set = set()
    for p in points:
        seen_ids.add(p["paper_id"])
        if not p.get("text"):
            continue
        if p["aspect"] == "other" and not include_other:
            continue
        by_aspect.setdefault(p["aspect"], []).append(p)

    neighbor_ids = all_paper_ids if all_paper_ids is not None else seen_ids

    patterns = []
    for aspect, items in by_aspect.items():
        paper_ids = {it["paper_id"] for it in items}
        if len(paper_ids) < min_papers:
            continue
        # 대표 문장: 가장 긴(= 구체적인) 지적을 예시 앞에 둔다
        items_sorted = sorted(items, key=lambda it: len(it["text"]), reverse=True)
        # 서로 다른 논문에서 예시를 뽑아 다양성 확보
        examples, seen = [], set()
        for it in items_sorted:
            if it["paper_id"] not in seen:
                examples.append(it["text"][:160])
                seen.add(it["paper_id"])
            if len(examples) >= 3:
                break

        pattern = ReviewPattern(
            label=ASPECT_LABELS.get(aspect, aspect),
            aspect=aspect,
            paper_count=len(paper_ids),
            total_papers=total_papers,
            examples=examples,
        )
        _attach_lift(pattern, base_rates.get(aspect))
        _attach_contrast(pattern, paper_ids, neighbor_ids, decisions)
        patterns.append(pattern)

    # 두드러진 것 먼저, 그다음 lift, 마지막으로 빈도.
    # base_rates가 없으면 lift가 전부 None이라 자연히 빈도순으로 떨어진다.
    patterns.sort(key=lambda p: (p.is_distinctive, p.lift or 0.0, p.paper_count),
                  reverse=True)
    return patterns[:top_k]


def _attach_lift(pattern: ReviewPattern, base_rate: float | None) -> None:
    """코퍼스 base rate 대비 lift와 이항검정 p값을 채운다."""
    n = pattern.total_papers
    if base_rate is None or not 0.0 < base_rate < 1.0 or n <= 0:
        return
    observed = pattern.paper_count / n
    pattern.base_rate = round(base_rate, 4)
    pattern.lift = round(observed / base_rate, 2)

    upper = observed >= base_rate
    pattern.p_value = round(
        binom_tail(pattern.paper_count, n, base_rate, upper=upper), 4)
    pattern.is_distinctive = (pattern.lift >= MIN_LIFT
                             and pattern.p_value <= MAX_P_VALUE)


def _attach_contrast(pattern: ReviewPattern, paper_ids: set, neighbor_ids: set,
                     decisions: dict) -> None:
    """이 지적을 받은 이웃 vs 받지 않은 이웃의 accept율을 채운다."""
    if not decisions:
        return
    aw, dw, ao, do = _accept_contrast(paper_ids, neighbor_ids, decisions)
    pattern.accept_with, pattern.decided_with = aw, dw
    pattern.accept_without, pattern.decided_without = ao, do
    if dw:
        pattern.accept_rate_with = round(aw / dw, 3)
    if do:
        pattern.accept_rate_without = round(ao / do, 3)
    if dw and do:
        pattern.contrast_p_value = round(
            fisher_exact_less(aw, dw - aw, ao, do - ao), 4)
        pattern.is_contrast_significant = (
            pattern.contrast_p_value <= MAX_P_VALUE
            and pattern.accept_rate_with < pattern.accept_rate_without)


def _greedy_cluster(vectors: np.ndarray, threshold: float) -> list[list[int]]:
    """정규화된 벡터들을 그리디하게 묶는다. 각 클러스터는 인덱스 리스트.

    범용 유틸. 현재 리뷰 패턴 집계에는 쓰지 않지만(§14), 향후 aspect 내부
    세분화 등에 재사용할 수 있다.
    """
    n = len(vectors)
    assigned = [False] * n
    sim = vectors @ vectors.T
    clusters = []
    order = np.argsort(-(sim >= threshold).sum(axis=1))
    for seed in order:
        if assigned[seed]:
            continue
        members = [int(seed)]
        assigned[seed] = True
        for j in range(n):
            if not assigned[j] and sim[seed, j] >= threshold:
                members.append(j)
                assigned[j] = True
        clusters.append(members)
    return clusters
