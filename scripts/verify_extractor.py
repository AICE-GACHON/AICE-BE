"""휴리스틱 추출기를 실제 ICLR 리뷰로 검증.

품질 지표: 리뷰당 추출 항목 수, aspect 분포, 'other' 비율.
'other'가 너무 높으면 키워드 사전을 보강하거나 Haiku로 전환할 신호.
"""
import logging
from collections import Counter

from paper_assistant.ingest.normalize import normalize_paper
from paper_assistant.ingest.openreview_client import VENUE_REGISTRY, get_client
from paper_assistant.ingest.review_extractor import HeuristicExtractor

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

N_PAPERS = 100
base, inv = VENUE_REGISTRY["ICLR 2024"]
client = get_client(base)
extractor = HeuristicExtractor()

print(f"ICLR 2024 논문 {N_PAPERS}편 수집·추출 중...\n")

aspect_counter = Counter()
sentiment_counter = Counter()
points_per_review = []
n_reviews = 0
examples = []

collected = 0
for note in client.iter_notes(invitation=inv):
    replies = client.get_forum_replies(note["forum"])
    paper = normalize_paper(note, replies, "ICLR 2024", 2024)
    if not paper.reviews:
        continue
    for review in paper.reviews:
        points = extractor.extract(review)
        n_reviews += 1
        points_per_review.append(len(points))
        for p in points:
            aspect_counter[p.aspect] += 1
            sentiment_counter[p.sentiment] += 1
            if len(examples) < 8 and p.aspect != "other":
                examples.append(p)
    collected += 1
    if collected >= N_PAPERS:
        break

total_points = sum(aspect_counter.values())
print(f"{'='*64}")
print(f"리뷰 {n_reviews}건 → 지적항목 {total_points}개 "
      f"(리뷰당 평균 {total_points/max(n_reviews,1):.1f}개)")
print(f"{'='*64}\n")

print("aspect 분포:")
for aspect, cnt in aspect_counter.most_common():
    bar = "█" * int(40 * cnt / total_points)
    print(f"  {aspect:22} {cnt:5} ({cnt/total_points*100:4.1f}%) {bar}")

other_pct = aspect_counter['other'] / total_points * 100
print(f"\nsentiment 분포: {dict(sentiment_counter)}")
print(f"\n{'='*64}")
print(f"'other' 비율: {other_pct:.1f}%", end="  ")
if other_pct > 55:
    print("⚠️  높음 — 키워드 보강 또는 Haiku 전환 고려")
elif other_pct > 40:
    print("△ 보통 — 클러스터링엔 쓸 만하나 aspect 집계는 제한적")
else:
    print("✅ 양호 — aspect 기반 집계 가능")
print(f"{'='*64}\n")

print("분류된 지적항목 예시:")
for p in examples:
    print(f"  [{p.aspect}] {p.text[:75]}")
