"""검색·집계 정확도 평가 — 코퍼스를 정답지로 쓰는 held-out 평가.

**아이디어**: 코퍼스 논문 P를 검색에서 제외하고 P의 제목+초록을 쿼리로 넣는다.
파이프라인이 내놓은 예상 지적을 **P가 실제로 받은 지적**과 대조한다. 사람이
라벨링할 필요가 없다 — 43,515편이 전부 정답이 달린 시험 문제다.

**왜 필요한가**: 이게 없으면 "창문을 넓히면 나아지나", "그래프 채널이 도움이
되나", "ICML을 넣으면 좋아지나"를 전부 눈대중으로 판단하게 된다. 이 스크립트는
그 질문들을 숫자로 바꾼다.

**반드시 봐야 할 숫자는 base rate 베이스라인과의 차이다.** 코퍼스의 78.8%가
baselines 지적을 받으므로(§18), 검색을 아예 안 하고 흔한 aspect 3개만 찍어도
꽤 맞는다. 파이프라인이 그걸 못 이기면 검색·lift·Fisher가 값을 못 하는 것이다.
그래서 모델 점수만 보지 말고 **lift(모델/베이스라인)를 보라.**

    python scripts/eval_retrieval.py --n 200
    python scripts/eval_retrieval.py --n 200 --top-k 100   # 창문 확대 비교

같은 --seed면 같은 표본이라 설정 간 비교가 성립한다. LLM은 쓰지 않는다 —
평가 대상이 검색·집계 층이기 때문이다($0).
"""
import argparse
import logging
import random
from collections import Counter

from paper_assistant.db.connection import cursor
from paper_assistant.db.stats import load_base_rates
from paper_assistant.embedding.specter2 import Specter2Embedder
from paper_assistant.graph.clustering import aggregate_by_aspect
from paper_assistant.retrieval.hybrid_search import hybrid_search

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("eval")

# 검색 결과에서 정답 논문과 그 중복본을 걸러내고도 top_k를 채우려면 여유가 필요하다.
EXCLUDE_MARGIN = 10


# --------------------------------------------------------------- 표본 추출
def sample_heldout(n: int, seed: int) -> list[dict]:
    """평가 대상 논문. **모든 리뷰가 분리 포맷인 논문만** 쓴다.

    미분리 리뷰는 본문 전체가 weakness로 라벨링돼 있어 aspect가 실제보다 부풀고
    (요약·칭찬까지 지적으로 셈) 정답지가 오염된다. 파이프라인의 '측정 가능한
    이웃' 정의와 같은 기준이다.
    """
    with cursor() as cur:
        cur.execute("""
            SELECT p.id, p.title, p.abstract, p.venue
            FROM papers p
            WHERE p.abstract IS NOT NULL AND length(p.abstract) > 200
              AND NOT EXISTS (SELECT 1 FROM reviews r
                              WHERE r.paper_id = p.id AND r.needs_llm_split)
              AND (SELECT count(DISTINCT rp.aspect) FROM review_points rp
                   WHERE rp.paper_id = p.id AND rp.sentiment = 'weakness'
                     AND rp.aspect <> 'other') >= 2
        """)
        rows = cur.fetchall()

    by_venue: dict[str, list] = {}
    for pid, title, abstract, venue in rows:
        by_venue.setdefault(venue, []).append(
            {"id": pid, "title": title, "abstract": abstract, "venue": venue})

    rng = random.Random(seed)
    per = max(1, n // max(1, len(by_venue)))
    out = []
    for venue in sorted(by_venue):                 # venue 고르게 (연도 편향 방지)
        group = by_venue[venue]
        rng.shuffle(group)
        out.extend(group[:per])
    rng.shuffle(out)
    return out[:n]


def leak_set(paper_id: int, title: str) -> set[int]:
    """정답 논문 자신 + 누출 경로.

    같은 논문을 다른 학회에 다시 낸 것(submission_links)과 제목이 같은 것은
    리뷰가 사실상 겹치므로 제외하지 않으면 정답을 보고 푸는 셈이 된다.
    """
    ids = {paper_id}
    with cursor() as cur:
        cur.execute("""
            SELECT earlier_paper_id, later_paper_id FROM submission_links
            WHERE earlier_paper_id = %s OR later_paper_id = %s
        """, (paper_id, paper_id))
        for a, b in cur.fetchall():
            ids.update((a, b))
        cur.execute("SELECT id FROM papers WHERE lower(title) = lower(%s)", (title,))
        ids.update(r[0] for r in cur.fetchall())
    return ids


# ----------------------------------------------------------------- 정답/예측
def actual_aspects(paper_id: int) -> set[str]:
    with cursor() as cur:
        cur.execute("""
            SELECT DISTINCT aspect FROM review_points
            WHERE paper_id = %s AND sentiment = 'weakness' AND aspect <> 'other'
        """, (paper_id,))
        return {r[0] for r in cur.fetchall()}


def fetch_points(paper_ids: list[int]) -> list[dict]:
    if not paper_ids:
        return []
    with cursor() as cur:
        cur.execute("""
            SELECT rp.paper_id, rp.aspect, rp.text
            FROM review_points rp
            WHERE rp.paper_id = ANY(%s) AND rp.sentiment = 'weakness'
        """, (paper_ids,))
        return [{"paper_id": r[0], "aspect": r[1], "text": r[2]}
                for r in cur.fetchall()]


def random_neighbors(paper_id: int, top_k: int, rng) -> list[int]:
    """대조군: 검색 대신 무작위 논문 top_k편.

    '의미적으로 가까운 논문이 비슷한 지적을 받는다'는 전제를 검증한다. 검색이
    무작위와 비슷하면, 이 용도에서 검색은 기여하지 않는다는 뜻이다.
    """
    with cursor() as cur:
        cur.execute("SELECT id FROM papers TABLESAMPLE SYSTEM (2) LIMIT 2000")
        pool = [r[0] for r in cur.fetchall() if r[0] != paper_id]
    rng.shuffle(pool)
    return pool[:top_k]


def predict(paper: dict, embedder, base_rates: dict, top_k: int,
            rng=None) -> list[str]:
    """held-out 쿼리에 대해 파이프라인이 내놓는 aspect 순위.

    rng를 주면 검색 대신 무작위 이웃을 쓴다 (대조군).
    """
    if rng is not None:
        ids = random_neighbors(paper["id"], top_k, rng)
        if not ids:
            return []
        patterns = aggregate_by_aspect(
            fetch_points(ids), total_papers=len(ids), base_rates=base_rates,
            all_paper_ids=set(ids))
        return [p.aspect for p in patterns]

    qvec = embedder.encode_one(paper["title"], paper["abstract"]).numpy()
    text = f"{paper['title']} {paper['abstract']}"
    hits = hybrid_search(qvec, text, top_k=top_k + EXCLUDE_MARGIN)

    banned = leak_set(paper["id"], paper["title"])
    neighbors = [h for h in hits if h.paper_id not in banned][:top_k]
    if not neighbors:
        return []

    ids = [h.paper_id for h in neighbors]
    patterns = aggregate_by_aspect(
        fetch_points(ids), total_papers=len(ids), base_rates=base_rates,
        decisions={h.paper_id: h.decision for h in neighbors},
        all_paper_ids=set(ids))
    return [p.aspect for p in patterns]


# -------------------------------------------------------------------- 지표
def prf(pred: list[str], truth: set[str], k: int) -> tuple[float, float, float]:
    top = set(pred[:k])
    if not top:
        return 0.0, 0.0, 0.0
    hit = len(top & truth)
    prec = hit / len(top)
    rec = hit / len(truth) if truth else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return prec, rec, f1


def main() -> None:
    ap = argparse.ArgumentParser(description="검색·집계 정확도 held-out 평가")
    ap.add_argument("--n", type=int, default=200, help="평가할 논문 수")
    ap.add_argument("--top-k", type=int, default=20, help="이웃 논문 수")
    ap.add_argument("--at", type=int, default=3, help="precision/recall@k의 k")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-base-rate", type=float, default=1.0,
                    help="이 값보다 base rate가 높은 aspect는 정답·예측·베이스라인에서 "
                         "모두 제외한다. 0.5로 주면 '흔하지 않은 지적'만 놓고 "
                         "겨루므로, lift 기계가 제 일을 하는지 공정하게 볼 수 있다.")
    args = ap.parse_args()

    base_rates = load_base_rates()
    if not base_rates:
        log.warning("aspect_base_rates가 비어 있습니다 — lift 없이 빈도순으로 돕니다.")

    # 평가 대상 aspect. --max-base-rate로 흔한 것을 빼면 "두드러진 지적을
    # 골라내는가"를 재게 된다 — 그게 §18이 설계한 목표다.
    eligible = {a for a, br in base_rates.items()
                if a != "other" and br <= args.max_base_rate}
    if not eligible:
        eligible = {a for a in base_rates if a != "other"}

    # 검색을 전혀 하지 않는 베이스라인: 평가 대상 중 가장 흔한 aspect를 늘 찍는다.
    baseline = [a for a, _ in sorted(base_rates.items(), key=lambda kv: -kv[1])
                if a in eligible][:args.at]

    papers = sample_heldout(args.n, args.seed)
    print(f"표본 {len(papers)}편 (venue별 균등, seed={args.seed}) · "
          f"top_k={args.top_k} · @{args.at} · "
          f"평가 aspect {sorted(eligible)}")
    print(f"베이스라인(검색 없음): {baseline}\n")

    embedder = Specter2Embedder()
    ctrl_rng = random.Random(args.seed + 99)
    m_scores, b_scores, r_scores, per_aspect = [], [], [], Counter()
    truth_sizes, skipped = [], 0

    for i, paper in enumerate(papers, 1):
        truth = actual_aspects(paper["id"]) & eligible
        if not truth:
            skipped += 1
            continue
        pred = [a for a in predict(paper, embedder, base_rates, args.top_k)
                if a in eligible]
        if not pred:
            skipped += 1
            continue

        rnd = [a for a in predict(paper, embedder, base_rates, args.top_k,
                                  rng=ctrl_rng) if a in eligible]
        m_scores.append(prf(pred, truth, args.at))
        b_scores.append(prf(baseline, truth, args.at))
        r_scores.append(prf(rnd, truth, args.at))
        truth_sizes.append(len(truth))
        for a in set(pred[:args.at]) & truth:
            per_aspect[a] += 1
        if i % 25 == 0:
            print(f"  … {i}/{len(papers)}", flush=True)

    n = len(m_scores)
    if not n:
        print("평가할 표본이 없습니다."); return

    def avg(scores, j):
        return sum(s[j] for s in scores) / len(scores)

    print(f"\n{'='*62}")
    print(f"유효 표본 {n}편 (건너뜀 {skipped}) · 논문당 실제 aspect "
          f"평균 {sum(truth_sizes)/n:.1f}개")
    print(f"{'='*62}")
    print(f"{'':10} {'precision':>10} {'recall':>10} {'F1':>10}")
    for name, sc in (("베이스라인", b_scores), ("무작위이웃", r_scores),
                     ("모델", m_scores)):
        print(f"{name:10} {avg(sc,0):>10.3f} {avg(sc,1):>10.3f} {avg(sc,2):>10.3f}")
    bf, mf, rf = avg(b_scores, 2), avg(m_scores, 2), avg(r_scores, 2)
    if bf:
        print(f"\n>>> 모델 / 베이스라인 : {mf/bf:.3f}배   "
              f"(1.0 미만이면 검색 없이 흔한 aspect만 찍는 게 낫다)")
    if rf:
        print(f">>> 모델 / 무작위이웃 : {mf/rf:.3f}배   "
              f"(1.0 근처면 '비슷한 논문은 비슷한 지적을 받는다'가 성립하지 않는다)")

    print(f"\n적중 분포 (모델이 상위 {args.at}개로 맞춘 aspect):")
    for a, c in per_aspect.most_common():
        print(f"  {a:22} {c:>4}회  ({c/n*100:4.1f}%)")


if __name__ == "__main__":
    main()
