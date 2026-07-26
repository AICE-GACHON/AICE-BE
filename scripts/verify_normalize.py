"""정규화 레이어를 10개 venue 실데이터로 검증한다.

각 venue에서 논문 3편씩 받아 정규화 후, 필드가 제대로 채워졌는지 점검.
"""
import logging

from paper_assistant.ingest.normalize import normalize_paper
from paper_assistant.ingest.openreview_client import VENUE_REGISTRY, get_client

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

SAMPLES_PER_VENUE = 3

print(f"{'venue':14} {'decision':14} {'리뷰':>4} {'rating':>7} {'weak자수':>8} "
      f"{'split':>6} {'초록':>5} {'meta':>5}")
print("-" * 76)

problems = []
for venue_name, (base, inv) in VENUE_REGISTRY.items():
    client = get_client(base)
    year = int(venue_name.split()[-1])

    subs = []
    for note in client.iter_notes(invitation=inv):
        subs.append(note)
        if len(subs) >= SAMPLES_PER_VENUE:
            break

    for sub in subs:
        replies = client.get_forum_replies(sub["forum"])
        p = normalize_paper(sub, replies, venue_name, year)

        n_rev = len(p.reviews)
        ratings = [r.rating for r in p.reviews if r.rating is not None]
        rating_str = f"{sum(ratings)/len(ratings):.1f}" if ratings else "-"
        weak_lens = [len(r.weaknesses) for r in p.reviews]
        avg_weak = sum(weak_lens) // len(weak_lens) if weak_lens else 0
        n_split = sum(1 for r in p.reviews if r.needs_llm_split)

        print(f"{venue_name:14} {p.decision:14} {n_rev:>4} {rating_str:>7} "
              f"{avg_weak:>8} {n_split:>3}/{n_rev:<2} {len(p.abstract):>5} "
              f"{len(p.meta_review):>5}")

        # 이상 징후 수집
        if not p.title:
            problems.append(f"{venue_name}: title 없음 ({p.openreview_id})")
        if not p.abstract:
            problems.append(f"{venue_name}: abstract 없음 ({p.openreview_id})")
        if p.decision == "unknown":
            problems.append(f"{venue_name}: decision 판별 실패 ({p.openreview_id})")
        if n_rev and not ratings:
            problems.append(f"{venue_name}: rating 파싱 실패 ({p.openreview_id})")
        if n_rev and avg_weak == 0:
            problems.append(f"{venue_name}: 리뷰 본문 추출 실패 ({p.openreview_id})")
        if n_rev and not p.author_ids:
            problems.append(f"{venue_name}: author_ids 없음 ({p.openreview_id})")
    print()

print("=" * 76)
if problems:
    print(f"발견된 문제 {len(problems)}건:")
    for p in problems:
        print(f"  - {p}")
else:
    print("모든 venue에서 정규화 정상 — 문제 없음")
