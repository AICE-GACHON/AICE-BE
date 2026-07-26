"""전체 수집 대상 규모 집계 (v1/v2 분기)."""
import logging

from paper_assistant.ingest.openreview_client import VENUE_REGISTRY, V1, get_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

total = 0
print(f"\n{'venue':16} {'API':4} {'논문 수':>8}")
print("-" * 32)
for name, (base, inv) in VENUE_REGISTRY.items():
    n = get_client(base).count_notes(invitation=inv)
    total += n
    print(f"{name:16} {'v1' if base == V1 else 'v2':4} {n:>8,}")
print("-" * 32)
print(f"{'합계':14} {'':4} {total:>8,}")
print(f"\n리뷰 추정 (편당 3.5건): {int(total * 3.5):,}건")
