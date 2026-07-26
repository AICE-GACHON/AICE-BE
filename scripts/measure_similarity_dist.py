"""SPECTER2 유사도 분포 측정.

무관한 논문쌍의 유사도가 얼마나 높게 나오는지를 재서,
절대 임계값을 쓸 수 있는지 / top-K 순위만 써야 하는지를 판단한다.
결과는 검색 설계와 프론트 표시 방식(유사도 % 노출 여부)에 직결된다.
"""
import logging
import random

import torch

from paper_assistant.embedding.specter2 import Specter2Embedder
from paper_assistant.ingest.normalize import normalize_paper
from paper_assistant.ingest.openreview_client import VENUE_REGISTRY, get_client

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

N_PAPERS = 300
base, inv = VENUE_REGISTRY["ICLR 2024"]
client = get_client(base)

print(f"ICLR 2024 논문 {N_PAPERS}편 수집 중...")
papers = []
for note in client.iter_notes(invitation=inv):
    p = normalize_paper(note, [], "ICLR 2024", 2024)
    if p.title and p.abstract:
        papers.append(p)
    if len(papers) >= N_PAPERS:
        break

print(f"임베딩 중 ({len(papers)}편)...")
embedder = Specter2Embedder()
vecs = embedder.encode([(p.title, p.abstract) for p in papers], batch_size=32)

sim = vecs @ vecs.T
n = len(papers)
mask = ~torch.eye(n, dtype=torch.bool)
pairs = sim[mask]  # 자기 자신 제외한 모든 쌍

qs = torch.tensor([0.01, 0.05, 0.25, 0.50, 0.75, 0.95, 0.99, 0.999])
vals = torch.quantile(pairs, qs)

print(f"\n{'='*62}")
print(f"무작위 논문쌍 {len(pairs):,}개의 코사인 유사도 분포")
print(f"{'='*62}")
print(f"  최소 {pairs.min():.4f} / 평균 {pairs.mean():.4f} / 최대 {pairs.max():.4f}")
print(f"  표준편차 {pairs.std():.4f}")
print()
for q, v in zip(qs.tolist(), vals.tolist()):
    print(f"  {q*100:6.1f} 분위 : {v:.4f}")

# 유사도 0.84(앞선 '무관쌍' 값)가 실제로 어느 분위인지
for probe in (0.80, 0.84, 0.88, 0.90, 0.92, 0.95):
    pct = (pairs < probe).float().mean().item() * 100
    print(f"\n  유사도 {probe:.2f} = 상위 {100-pct:.1f}% (하위 {pct:.1f}%보다 유사)")

print(f"\n{'='*62}")
print("해석")
print(f"{'='*62}")
p50, p99 = vals[3].item(), vals[6].item()
print(f"  무작위 쌍의 중앙값이 이미 {p50:.3f} — 절대값 자체는 의미가 없다.")
print(f"  상위 1%조차 {p99:.3f}이므로, '0.85 이상 = 유사'같은 임계값은 무의미.")
print(f"  → 검색은 반드시 top-K 순위 기반. RRF 하이브리드 설계가 이 특성과 맞다.")
print(f"  → 프론트에 '유사도 {p50*100:.0f}%'로 표기하면 사용자가 오해한다.")
print(f"     순위 또는 그룹 내 상대 점수로 표시할 것.")
