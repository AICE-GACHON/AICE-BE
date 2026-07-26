"""LangGraph 파이프라인 배선 검증 ($0, LLM off).

DB에 이미 적재된 논문으로 analyze()를 끝까지 돌려 Report 구조를 확인한다.
크레딧을 쓰지 않으므로 (use_llm=False) 태깅·종합 요약은 스텁이지만,
검색→병렬 분석→종합의 DAG 흐름과 Report 스키마를 검증할 수 있다.
"""
import logging

from paper_assistant import analyze

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# 알려진 주제로 쿼리 (그래프 신경망 — 파일럿 데이터에 유사 논문 있음)
report = analyze(
    title="Graph Neural Networks for Molecular Property Prediction",
    abstract=("We propose a message-passing graph neural network for predicting "
              "molecular properties, evaluated on quantum chemistry benchmarks "
              "with strong baselines and ablation studies."),
    use_llm=False,   # $0 — 스텁 태깅/요약
)

print(f"\n{'='*70}")
print(f"쿼리: {report.query_title}")
print(f"{'='*70}")

c = report.confidence
print(f"\n[검색 신뢰도] {c.level.upper()} — {c.message}")
if not c.is_reliable:
    print("  ※ 아래 결과는 신뢰할 수 없습니다.")

MATCH_LABEL = {"both": "의미+용어", "semantic": "의미만", "lexical": "용어만"}
print(f"\n[유사 논문 {len(report.similar_papers)}편]")
for p in report.similar_papers[:8]:
    if p.avg_rating is not None:
        vs = f"{p.rating_vs_venue:+.1f}" if p.rating_vs_venue is not None else "  ? "
        score = f"{p.avg_rating:4.1f}({vs}) x{p.rating_count}"
    else:
        score = "점수없음"
    print(f"  {p.rank:2}. [{MATCH_LABEL[p.match_type]:>6}] {score:>18}  "
          f"{p.decision:14} {p.title[:38]}")
    for t in p.tags:
        print(f"        · {t.kind}: {t.reason}")

rc = report.rating_context
print(f"\n[리뷰 점수 맥락]  ※ 점수 옆 괄호는 같은 venue 평균 대비")
if rc.rated_papers:
    print(f"  이웃 평균 {rc.neighbor_mean} ({rc.rated_papers}편) — "
          f"통과 {rc.accepted_mean} vs 탈락 {rc.rejected_mean}")
    if rc.threshold is not None:
        print(f"  당락 경계: {rc.threshold_venue} 기준 평균 {rc.threshold} 이상 "
              f"→ 통과율 50% 초과")
    for t in rc.split_papers:
        print(f"  리뷰어 의견 갈림: {t[:56]}")
    for v in rc.biased_venues:
        print(f"  ⚠️ {v}: 채택 논문 위주 공개 — accept율을 실제 채택률로 읽지 말 것")
else:
    print("  점수 데이터 없음")

print(f"\n[리뷰 지적 패턴 {len(report.review_patterns)}개]  "
      f"★=이 주제 특유 (lift≥1.25, p≤0.05)")
print(f"  {'':2}{'지적':<16}{'관측':>8}{'코퍼스':>8}{'lift':>7}{'p':>8}   당락 대조")
for pat in report.review_patterns:
    mark = "★" if pat.is_distinctive else " "
    base = f"{pat.base_rate*100:.0f}%" if pat.base_rate else "-"
    lift = f"{pat.lift:.2f}" if pat.lift else "-"
    pval = f"{pat.p_value:.3f}" if pat.p_value is not None else "-"
    obs = f"{pat.paper_count}/{pat.total_papers}"
    if pat.accept_rate_with is not None and pat.accept_rate_without is not None:
        sig = "✔" if pat.is_contrast_significant else " "
        contrast = (f"{sig} 지적받음 {pat.accept_with}/{pat.decided_with} "
                    f"({pat.accept_rate_with*100:.0f}%) vs "
                    f"미지적 {pat.accept_without}/{pat.decided_without} "
                    f"({pat.accept_rate_without*100:.0f}%)  p={pat.contrast_p_value}")
    else:
        contrast = "-"
    print(f"  {mark} {pat.label:<16}{obs:>8}{base:>8}{lift:>7}{pval:>8}   {contrast}")

print(f"\n[게재 경향 {len(report.venue_trends)}개]")
for t in report.venue_trends:
    base = (f" · 코퍼스 {t.corpus_accept_rate*100:.0f}% 대비 {t.accept_lift:.2f}배"
            if t.accept_lift else "")
    warn = "  ⚠️표본편향" if t.is_coverage_biased else ""
    print(f"  {t.venue}: {t.accept_count}/{t.paper_count} accept "
          f"({t.accept_rate*100:.0f}%){base}{warn}")

print(f"\n[종합 요약]\n{report.summary_markdown}")

print(f"\n{'='*70}")
print("✅ 파이프라인 end-to-end 정상 (LLM off, $0)")
print(f"{'='*70}")
