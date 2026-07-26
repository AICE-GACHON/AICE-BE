"""검색 신뢰도 판정 검증 ($0, LLM off).

도메인 안/밖 쿼리를 나란히 돌려 `RetrievalConfidence`가 제 역할을 하는지 본다.
이 판정이 없으면 "치즈 숙성 미생물학"에도 ML 논문 20편을 자신있게 내놓는다
(설계서 §20).
"""
import logging

from paper_assistant import analyze

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

CASES = [
    ("도메인 안", "Graph Neural Networks for Molecular Property Prediction",
     "We propose a message-passing graph neural network for predicting molecular "
     "properties, evaluated on quantum chemistry benchmarks."),
    ("도메인 안", "Low-Rank Adaptation of Large Language Models",
     "We freeze the pretrained model weights and inject trainable rank "
     "decomposition matrices into each layer of the Transformer."),
    ("도메인 밖", "A Study of Cheese Ripening Microbiology",
     "We characterize bacterial succession during the ripening of alpine cheeses "
     "using 16S rRNA sequencing."),
    ("도메인 밖", "Medieval Trade Routes of the Hanseatic League",
     "Commercial networks and maritime law in the Baltic between 1250 and 1450."),
]

print(f"\n{'기대':8}{'판정':10}{'근거(cos)':>10}  {'신뢰':5}  쿼리")
print("-" * 88)
for expected, title, abstract in CASES:
    report = analyze(title=title, abstract=abstract, use_llm=False)
    c = report.confidence
    ok = "✅" if c.is_reliable == (expected == "도메인 안") else "❌"
    print(f"{expected:8}{c.level:10}{c.evidence:10.4f}  {ok:5}  {title[:44]}")

print("\n--- 도메인 밖 쿼리의 요약 (경고가 맨 앞에 오는지) ---")
report = analyze(title=CASES[-1][1], abstract=CASES[-1][2], use_llm=False)
print(report.summary_markdown)
print(f"\n상위 3편 (신뢰할 수 없는 결과):")
for p in report.similar_papers[:3]:
    print(f"  {p.rank}. [{p.match_type}] {p.title[:60]}")
