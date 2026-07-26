"""토글 ON(use_llm=True)으로 쿼리를 재현해 실제 LLM 출력을 뽑는다.

정성 평가용 — Haiku 태깅 + Sonnet 종합이 실제로 어떻게 나오는지 전부 출력.
비용: 약 $0.05 (Haiku 태깅 ~10콜 + Sonnet 종합 1콜).
"""
import logging

from paper_assistant import analyze

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

# 화면의 LSTM 쿼리 재현 (Hochreiter & Schmidhuber 1997 원 초록)
TITLE = "Long Short-Term Memory"
ABSTRACT = (
    "Learning to store information over extended time intervals via recurrent "
    "backpropagation takes a very long time, mostly due to insufficient, decaying "
    "error back flow. We briefly review Hochreiter's 1991 analysis of this problem, "
    "then address it by introducing a novel, efficient, gradient-based method called "
    "long short-term memory (LSTM). Truncating the gradient where this does not do "
    "harm, LSTM can learn to bridge minimal time lags in excess of 1000 discrete time "
    "steps by enforcing constant error flow through constant error carousels within "
    "special units. Multiplicative gate units learn to open and close access to the "
    "constant error flow. LSTM is local in space and time; its computational complexity "
    "per time step and weight is O(1). In comparisons with RTRL, BPTT, Recurrent "
    "Cascade-Correlation, Elman nets, and Neural Sequence Chunking, LSTM leads to many "
    "more successful runs, and learns much faster.")

report = analyze(TITLE, ABSTRACT, use_llm=True)

print("=" * 78)
print(f"쿼리: {report.query_title}")
print("=" * 78)

print("\n[유사 논문 + LLM 태깅]  (상위 10편만 태깅됨)")
for p in report.similar_papers[:10]:
    print(f"\n {p.rank}. {p.title[:64]}")
    print(f"    {p.venue} · {p.decision}")
    if p.tags:
        for t in p.tags:
            print(f"    → [{t.kind}] {t.reason}")
    else:
        print(f"    (태그 없음)")

print("\n" + "=" * 78)
print("[리뷰 지적 패턴]")
for pat in report.review_patterns:
    print(f"  [{pat.aspect}] {pat.paper_count}/{pat.total_papers}편: {pat.label}")

print("\n[게재 경향]")
for t in report.venue_trends:
    print(f"  {t.venue}: {t.accept_count}/{t.paper_count} ({t.accept_rate*100:.0f}%)")
for f in report.resubmission_flows[:6]:
    print(f"  재투고: {f.from_venue} → {f.to_venue} ({f.count}건)")

print("\n" + "=" * 78)
print("[Sonnet 종합 요약]")
print("=" * 78)
print(report.summary_markdown)
