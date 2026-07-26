"""SPECTER2 임베딩 품질 검증.

1. 차원 실측 (설계서 §9 리스크 항목: 스키마의 vector(768) 확정)
2. 알려진 관련 논문쌍이 무관한 논문쌍보다 실제로 가까운지
3. 실제 ICLR 논문으로 유사도 검색이 말이 되는지 (정성 평가)
4. 처리 속도 측정 (43,515편 전체 임베딩 소요 시간 추정)
"""
import json
import logging
import time

import torch

from paper_assistant import config
from paper_assistant.embedding.specter2 import Specter2Embedder
from paper_assistant.ingest.normalize import normalize_paper

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

embedder = Specter2Embedder()
print(f"\n{'='*70}\n[1] 임베딩 차원: {embedder.dim}\n{'='*70}")

# --- [2] 관련쌍 vs 무관쌍 ---
PAIRS = {
    "관련 (둘 다 Transformer 구조)": (
        ("Attention Is All You Need",
         "The dominant sequence transduction models are based on complex recurrent or "
         "convolutional neural networks. We propose the Transformer, based solely on "
         "attention mechanisms, dispensing with recurrence and convolutions entirely."),
        ("BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
         "We introduce BERT, which stands for Bidirectional Encoder Representations from "
         "Transformers, designed to pretrain deep bidirectional representations from "
         "unlabeled text by jointly conditioning on both left and right context."),
    ),
    "무관 (NLP vs 단백질 구조)": (
        ("Attention Is All You Need",
         "The dominant sequence transduction models are based on complex recurrent or "
         "convolutional neural networks. We propose the Transformer, based solely on "
         "attention mechanisms."),
        ("Highly accurate protein structure prediction with AlphaFold",
         "Predicting the three-dimensional structure that a protein will adopt based "
         "solely on its amino acid sequence has been an open research problem for more "
         "than 50 years. Here we provide the first computational method AlphaFold."),
    ),
    "관련 (둘 다 tabular deep learning)": (
        ("TabR: Tabular Deep Learning Meets Nearest Neighbors",
         "Deep learning models for tabular data are actively studied. We propose TabR, "
         "a retrieval-augmented model that attends to nearest neighbors among training "
         "examples to improve tabular prediction."),
        ("Revisiting Deep Learning Models for Tabular Data",
         "We perform an overview of the main families of deep learning architectures for "
         "tabular data and raise the question of how they compare to gradient boosted "
         "decision trees on tabular problems."),
    ),
}

print("\n[2] 관련쌍 vs 무관쌍 코사인 유사도")
for label, (p1, p2) in PAIRS.items():
    v = embedder.encode([p1, p2])
    sim = float(v[0] @ v[1])  # 정규화되어 있으므로 내적 = 코사인
    print(f"    {sim:.4f}  {label}")

# --- [3] 실제 ICLR 논문으로 검색 ---
sample_path = config.RAW_DIR / "pilot_iclr2024" / "sample.json"
if sample_path.exists():
    recs = json.loads(sample_path.read_text(encoding="utf-8"))
    papers = [normalize_paper(r["submission"], r["replies"], "ICLR 2024", 2024)
              for r in recs]
    papers = [p for p in papers if p.title and p.abstract]

    print(f"\n[3] 실제 ICLR 2024 논문 {len(papers)}편 임베딩 후 유사도 검색")
    vecs = embedder.encode([(p.title, p.abstract) for p in papers])
    sim_matrix = vecs @ vecs.T
    sim_matrix.fill_diagonal_(-1)  # 자기 자신 제외

    query_idx = 0
    scores, idx = sim_matrix[query_idx].topk(min(3, len(papers) - 1))
    print(f"    쿼리: {papers[query_idx].title[:65]}")
    for rank, (s, i) in enumerate(zip(scores.tolist(), idx.tolist()), 1):
        print(f"      {rank}. {s:.4f}  {papers[i].title[:60]}")

    # 가장 유사한 쌍 전체
    flat = sim_matrix.flatten()
    top = flat.argmax().item()
    a, b = divmod(top, len(papers))
    print(f"\n    전체에서 가장 유사한 쌍 ({flat[top]:.4f}):")
    print(f"      A: {papers[a].title[:60]}")
    print(f"      B: {papers[b].title[:60]}")
else:
    print("\n[3] 건너뜀 — 먼저 run_pilot.py 실행 필요")

# --- [4] 속도 측정 ---
print("\n[4] 처리 속도")
bench = [("Test paper title number %d" % i,
          "This is a benchmark abstract with enough tokens to be realistic. " * 8)
         for i in range(32)]
start = time.perf_counter()
embedder.encode(bench, batch_size=16)
elapsed = time.perf_counter() - start
per_paper = elapsed / len(bench)
total_hours = per_paper * 43515 / 3600
print(f"    {len(bench)}편 {elapsed:.2f}초 → 편당 {per_paper*1000:.1f}ms")
print(f"    43,515편 전체 예상: {total_hours:.1f}시간 ({embedder.device})")
