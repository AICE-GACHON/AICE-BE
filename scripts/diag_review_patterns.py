"""리뷰 패턴 집계 진단 — 왜 검색이 base rate 베이스라인을 못 이기는가.

`eval_retrieval.py` 는 파이프라인이 베이스라인을 못 이긴다는 것까지만 알려준다.
이 스크립트는 **왜 그런지**를 분해한다. 진단 전용이라 아무것도 수정하지 않는다.

    python -m scripts.diag_review_patterns             # 전체
    python -m scripts.diag_review_patterns --mode beta --n 300

⚠️ `python scripts/diag_review_patterns.py` 로는 안 된다. 그렇게 실행하면 파이썬이
`scripts/` 를 sys.path 에 넣기 때문에 저장소 루트가 빠져 `paper_assistant` 를 못 찾는다.
저장소 루트에서 `-m` 으로 실행할 것 (기존 scripts/*.py 도 마찬가지다).

측정 4종:
  aspects   — aspect 분포와 분야별 편차. 라벨이 얼마나 흔한지, 분야로 얼마나 갈리는지
  baselines — 전역/분야 베이스라인 vs 파이프라인. **공정한 비교 대상은 분야 쪽**이다
  beta      — 정렬 기준을 확률(β=0) ↔ lift(β=1) 사이에서 훑는다
  topk      — 이웃 수를 늘리면 나아지는가

**왜 2025년 논문을 쿼리로 쓰나**: held-out 평가는 코퍼스 논문을 쿼리로 쓰는데, 옛
논문을 넣으면 정답이 그 시절 심사 기준이라 최신성이 부당하게 손해를 본다
(docs/랭킹_가중치_설계.md §11.4). 실사용에서 쿼리는 항상 새 논문이므로 가장 최신
연도가 실제 조건에 가깝다.

LLM 을 쓰지 않으므로 **비용은 $0**이다. 해석은 docs/리뷰패턴_진단.md 참고.
"""
import argparse
import logging
import random
import statistics
from collections import Counter

from paper_assistant.db.connection import cursor
from paper_assistant.db.stats import load_base_rates
from paper_assistant.graph.clustering import aggregate_by_aspect
from paper_assistant.retrieval.hybrid_search import hybrid_search
from scripts.eval_retrieval import EXCLUDE_MARGIN, leak_set, prf

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

BETAS = [0.0, 0.25, 0.5, 0.75, 1.0]
TOP_KS = [20, 50, 100, 150]


# ------------------------------------------------------------------ 공통
def sample_papers(n: int, seed: int, year: int) -> list[dict]:
    """평가 대상. eval_retrieval.sample_heldout 과 같은 품질 조건 + 연도·분야 제한.

    미분리 리뷰(2023년 이전)는 본문 전체가 weakness 로 라벨링돼 정답지가 오염되므로
    제외한다. primary_area 는 분야 베이스라인을 만들려면 필요하다.
    """
    with cursor() as cur:
        cur.execute("""
            SELECT p.id, p.title, p.abstract, p.primary_area
            FROM papers p
            WHERE p.year = %s
              AND p.abstract IS NOT NULL AND length(p.abstract) > 200
              AND p.embedding IS NOT NULL AND p.primary_area IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM reviews r
                              WHERE r.paper_id = p.id AND r.needs_llm_split)
              AND (SELECT count(DISTINCT rp.aspect) FROM review_points rp
                   WHERE rp.paper_id = p.id AND rp.sentiment = 'weakness'
                     AND rp.aspect <> 'other') >= 2
        """, (year,))
        rows = cur.fetchall()
    rng = random.Random(seed)
    rng.shuffle(rows)
    return [{"id": r[0], "title": r[1], "abstract": r[2], "area": r[3]}
            for r in rows[:n]]


def embeddings_for(paper_ids: list[int]) -> dict:
    with cursor() as cur:
        cur.execute("SELECT id, embedding FROM papers WHERE id = ANY(%s)",
                    (paper_ids,))
        return {r[0]: r[1] for r in cur.fetchall()}


def truths_for(paper_ids: list[int]) -> dict:
    with cursor() as cur:
        cur.execute("""
            SELECT paper_id, aspect FROM review_points
            WHERE paper_id = ANY(%s) AND sentiment = 'weakness' AND aspect <> 'other'
        """, (paper_ids,))
        out: dict[int, set] = {}
        for pid, aspect in cur.fetchall():
            out.setdefault(pid, set()).add(aspect)
        return out


def points_for(paper_ids: list[int]) -> list[dict]:
    if not paper_ids:
        return []
    with cursor() as cur:
        cur.execute("""
            SELECT paper_id, aspect, text FROM review_points
            WHERE paper_id = ANY(%s) AND sentiment = 'weakness' AND text IS NOT NULL
        """, (paper_ids,))
        return [{"paper_id": r[0], "aspect": r[1], "text": r[2]}
                for r in cur.fetchall()]


def area_rankings() -> dict[str, list[str]]:
    """분야별 aspect 순위 — '같은 분야에서 흔한 순'. 공정한 비교 대상이다."""
    with cursor() as cur:
        cur.execute("""
            WITH t AS (
              SELECT DISTINCT p.primary_area AS area, rp.aspect, rp.paper_id
              FROM review_points rp JOIN papers p ON p.id = rp.paper_id
              WHERE rp.sentiment = 'weakness' AND rp.aspect <> 'other'
                AND p.primary_area IS NOT NULL
            ), tot AS (SELECT area, count(DISTINCT paper_id) AS n FROM t GROUP BY area)
            SELECT t.area, t.aspect, count(DISTINCT t.paper_id)::float / tot.n
            FROM t JOIN tot ON tot.area = t.area
            GROUP BY t.area, t.aspect, tot.n
        """)
        by: dict[str, list] = {}
        for area, aspect, rate in cur.fetchall():
            by.setdefault(area, []).append((aspect, rate))
    return {a: [x for x, _ in sorted(v, key=lambda kv: kv[1], reverse=True)]
            for a, v in by.items()}


def rank_by_beta(patterns, base_rates: dict, beta: float) -> list[str]:
    """점수 = P / base^beta. beta=0 이면 확률순, 1 이면 현재(lift)순."""
    def score(p):
        prob = p.paper_count / p.total_papers if p.total_papers else 0.0
        base = base_rates.get(p.aspect) or 0.0
        return prob if base <= 0 else prob / (base ** beta)
    return [p.aspect for p in sorted(patterns, key=score, reverse=True)]


def neighbors_for(samples, embeds, want: int, pool: int) -> dict[int, list[int]]:
    """논문별 이웃 목록. **검색은 한 번만** 하고 앞에서 잘라 쓴다 —
    top_k 별 결과가 같은 순서를 공유해야 비교가 깨끗하다."""
    out = {}
    for s in samples:
        text = f"{s['title']} {s['abstract']}"
        hits = hybrid_search(embeds[s["id"]], text, top_k=want + EXCLUDE_MARGIN,
                             pool=pool)
        banned = leak_set(s["id"], s["title"])
        out[s["id"]] = [h.paper_id for h in hits
                        if h.paper_id not in banned][:want]
    return out


def mean_f1(samples, truths, preds, at: int) -> float:
    return statistics.mean(prf(p, truths[s["id"]], at)[2]
                           for s, p in zip(samples, preds))


# ------------------------------------------------------------------ 측정
def show_aspects() -> None:
    print("\n=== aspect 분포 — 라벨이 얼마나 흔한가 ===")
    with cursor() as cur:
        cur.execute("""
            SELECT aspect, count(DISTINCT paper_id) AS papers,
                   round(100.0 * count(DISTINCT paper_id) /
                         (SELECT count(DISTINCT paper_id) FROM review_points
                          WHERE sentiment = 'weakness'), 1) AS pct
            FROM review_points WHERE sentiment = 'weakness'
            GROUP BY aspect ORDER BY papers DESC
        """)
        print(f"{'aspect':<24}{'논문 수':>10}{'비율':>8}")
        print("-" * 42)
        for a, n, pct in cur.fetchall():
            print(f"{a:<24}{n:>10,}{pct:>7}%")
    print("\n상위 3개만 늘 찍어도 정밀도가 60% 근처다 — 지능 없이 나오는 점수다.")


def show_baselines(samples, truths, neighbors, base_rates) -> None:
    print("\n=== 베이스라인 비교 — 공정한 상대는 '분야' 쪽이다 ===")
    areas = area_rankings()
    global_ranked = [a for a, _ in sorted(
        ((a, r) for a, r in base_rates.items() if a != "other"),
        key=lambda kv: kv[1], reverse=True)]

    rows = [("전역 베이스라인", [global_ranked] * len(samples)),
            ("분야 베이스라인",
             [areas.get(s["area"], global_ranked) for s in samples])]
    pipeline = []
    for s in samples:
        ids = neighbors[s["id"]]
        pats = aggregate_by_aspect(points_for(ids), total_papers=len(ids) or 1,
                                   base_rates=base_rates, all_paper_ids=set(ids))
        pipeline.append([p.aspect for p in pats])
    rows.append(("파이프라인 (현재)", pipeline))

    print(f"{'':<22}{'F1@1':>9}{'F1@2':>9}{'F1@3':>9}")
    print("-" * 49)
    for label, preds in rows:
        f = [mean_f1(samples, truths, preds, at) for at in (1, 2, 3)]
        print(f"{label:<22}{f[0]:>9.3f}{f[1]:>9.3f}{f[2]:>9.3f}")


def show_beta(samples, truths, neighbors, base_rates) -> None:
    print("\n=== 정렬 기준 스윕 — 확률(β=0) ↔ lift(β=1) ===")
    pats_by_paper = {}
    for s in samples:
        ids = neighbors[s["id"]]
        pats_by_paper[s["id"]] = aggregate_by_aspect(
            points_for(ids), total_papers=len(ids) or 1,
            base_rates=base_rates, all_paper_ids=set(ids))

    print(f"{'':<22}{'F1@1':>9}{'F1@2':>9}{'F1@3':>9}")
    print("-" * 49)
    top1 = {}
    for beta in BETAS:
        preds = [rank_by_beta(pats_by_paper[s["id"]], base_rates, beta)
                 for s in samples]
        f = [mean_f1(samples, truths, preds, at) for at in (1, 2, 3)]
        tag = " (확률순)" if beta == 0 else (" (현재)" if beta == 1 else "")
        print(f"{f'β={beta:.2f}{tag}':<22}{f[0]:>9.3f}{f[1]:>9.3f}{f[2]:>9.3f}")
        top1[beta] = Counter(p[0] for p in preds if p)

    print("\n1위로 고른 aspect — 현재 방식은 '드문 것'을 앞에 놓는다")
    print(f"{'aspect':<24}{'코퍼스':>8}{'β=0':>8}{'β=1':>8}")
    print("-" * 48)
    n = len(samples)
    for aspect in sorted(base_rates, key=lambda a: -base_rates[a]):
        if aspect == "other":
            continue
        c0 = top1[0.0].get(aspect, 0) / n * 100
        c1 = top1[1.0].get(aspect, 0) / n * 100
        print(f"{aspect:<24}{base_rates[aspect]*100:>7.1f}%{c0:>7.1f}%{c1:>7.1f}%")


def show_topk(samples, truths, neighbors, base_rates) -> None:
    print("\n=== 이웃 수 스윕 — 더 많이 보면 나아지는가 (β=0 기준) ===")
    areas = area_rankings()
    global_ranked = [a for a, _ in sorted(
        ((a, r) for a, r in base_rates.items() if a != "other"),
        key=lambda kv: kv[1], reverse=True)]
    base = [mean_f1(samples, truths,
                    [areas.get(s["area"], global_ranked) for s in samples], at)
            for at in (1, 2, 3)]

    print(f"{'':<22}{'F1@1':>9}{'F1@2':>9}{'F1@3':>9}")
    print("-" * 49)
    print(f"{'[기준] 분야':<22}{base[0]:>9.3f}{base[1]:>9.3f}{base[2]:>9.3f}")
    for k in TOP_KS:
        preds = []
        for s in samples:
            ids = neighbors[s["id"]][:k]
            pats = aggregate_by_aspect(points_for(ids), total_papers=len(ids) or 1,
                                       base_rates=base_rates,
                                       all_paper_ids=set(ids)) if ids else []
            preds.append(rank_by_beta(pats, base_rates, 0.0))
        f = [mean_f1(samples, truths, preds, at) for at in (1, 2, 3)]
        mark = "  ← 넘음" if f[2] > base[2] + 0.005 else ""
        print(f"{f'이웃 {k}편':<22}{f[0]:>9.3f}{f[1]:>9.3f}{f[2]:>9.3f}{mark}")


# ------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser(description="리뷰 패턴 집계 진단 (동작 변경 없음)")
    ap.add_argument("--n", type=int, default=300, help="표본 논문 수")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--year", type=int, default=2025,
                    help="쿼리로 쓸 논문의 연도 (실사용 조건 = 최신)")
    ap.add_argument("--pool", type=int, default=200,
                    help="후보 풀. 이웃 수 스윕을 하려면 넉넉해야 한다")
    ap.add_argument("--mode", default="all",
                    choices=["all", "aspects", "baselines", "beta", "topk"])
    args = ap.parse_args()

    if args.mode in ("all", "aspects"):
        show_aspects()
    if args.mode == "aspects":
        return

    base_rates = load_base_rates()
    if not base_rates:
        raise SystemExit("aspect_base_rates가 비어 있습니다. lift 계산이 불가능합니다.")

    samples = sample_papers(args.n, args.seed, args.year)
    embeds = embeddings_for([s["id"] for s in samples])
    samples = [s for s in samples if embeds.get(s["id"]) is not None]
    truths = truths_for([s["id"] for s in samples])
    samples = [s for s in samples if truths.get(s["id"])]
    if not samples:
        raise SystemExit("평가 가능한 표본이 없습니다.")

    want = max(TOP_KS) if args.mode in ("all", "topk") else 20
    print(f"\n표본 {len(samples)}편 ({args.year}년 논문) / 후보 풀 {args.pool} / "
          f"이웃 최대 {want}편")
    neighbors = neighbors_for(samples, embeds, want, args.pool)

    if args.mode in ("all", "baselines"):
        show_baselines(samples, truths, {k: v[:20] for k, v in neighbors.items()},
                       base_rates)
    if args.mode in ("all", "beta"):
        show_beta(samples, truths, {k: v[:20] for k, v in neighbors.items()},
                  base_rates)
    if args.mode in ("all", "topk"):
        show_topk(samples, truths, neighbors, base_rates)

    print("\n해석은 docs/리뷰패턴_진단.md 참고.")


if __name__ == "__main__":
    main()
