"""ICLR 2024 파일럿 수집: 샘플 N편 + 리뷰 전체를 받아 구조를 파악한다.

사용법 (.env에 OpenReview 자격 증명 필요):
    python -m paper_assistant.ingest.run_pilot [샘플 수, 기본 20]
"""
import json
import logging
import sys
import time
from collections import Counter

from paper_assistant import config
from paper_assistant.ingest.openreview_client import VENUE_REGISTRY, get_client

VENUE_NAME = "ICLR 2024"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


def main(n_samples: int = 20):
    out_dir = config.RAW_DIR / "pilot_iclr2024"
    out_dir.mkdir(parents=True, exist_ok=True)

    base, invitation = VENUE_REGISTRY[VENUE_NAME]
    client = get_client(base)

    log.info("제출 논문 %d편 샘플 수집 중...", n_samples)
    submissions = []
    for note in client.iter_notes(invitation=invitation):
        submissions.append(note)
        if len(submissions) >= n_samples:
            break

    records = []
    for i, sub in enumerate(submissions, 1):
        replies = client.get_forum_replies(sub["forum"])
        records.append({"submission": sub, "replies": replies})
        log.info("[%d/%d] %s — 리플라이 %d건",
                 i, len(submissions),
                 sub["content"].get("title", {}).get("value", "?")[:60],
                 len(replies))
        time.sleep(0.5)  # rate limit 예방

    out_file = out_dir / "sample.json"
    out_file.write_text(json.dumps(records, indent=2), encoding="utf-8")
    log.info("저장: %s", out_file)

    # --- 구조 분석 요약 ---
    print("\n===== 구조 분석 =====")
    sub0 = records[0]["submission"]
    print(f"submission content 키: {sorted(sub0['content'].keys())}")

    inv_counter = Counter()
    review_keys = decision_values = None
    for rec in records:
        for reply in rec["replies"]:
            for inv in reply.get("invitations", []):
                kind = inv.split("/")[-1]
                inv_counter[kind] += 1
                if kind == "Official_Review" and review_keys is None:
                    review_keys = sorted(reply["content"].keys())
                if kind == "Decision":
                    decision_values = reply["content"].get("decision")
    print(f"리플라이 invitation 유형 분포: {dict(inv_counter)}")
    print(f"Official_Review content 키: {review_keys}")
    print(f"Decision 예시: {decision_values}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    main(n)
