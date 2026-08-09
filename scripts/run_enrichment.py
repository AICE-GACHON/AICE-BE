"""arXiv/S2 보강 전체 실행 — 하베스트 → arXiv 매칭 → S2 보강 → 재투고 재계산
→ 인용 백분위 재계산.

각 단계가 멱등이라 중간에 끊겨도 그냥 다시 실행하면 이어진다.

    python scripts/run_enrichment.py                  # 전 단계
    python scripts/run_enrichment.py --skip-harvest   # 캐시가 이미 있을 때

소요 시간(실측 기준): 하베스트가 압도적으로 길다(cs+stat 2018년 이후 ≈ 2~3시간,
1요청 1,300건/16초). 나머지는 합쳐서 10~20분.
"""
import argparse
import logging

from paper_assistant.ingest import (
    arxiv_client, arxiv_matcher, citation_percentile, s2_enricher)
from paper_assistant.ingest.s2_client import S2Client
from paper_assistant.ingest.submission_linker import run_linking

log = logging.getLogger("enrich-all")


def main() -> None:
    ap = argparse.ArgumentParser(description="arXiv/S2 보강 일괄 실행")
    ap.add_argument("--skip-harvest", action="store_true",
                    help="arXiv 하베스트를 건너뛴다 (캐시 재사용)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if not args.skip_harvest:
        log.info("[1/4] arXiv OAI-PMH 하베스트")
        arxiv_client.harvest()
    else:
        log.info("[1/4] 하베스트 생략 — 체크포인트: %s", arxiv_client.cache_status())

    log.info("[2/4] arXiv id 매칭")
    arxiv_matcher.run_matching()

    log.info("[3/5] Semantic Scholar 보강")
    client = S2Client()
    s2_enricher.enrich_by_arxiv(client)
    s2_enricher.enrich_by_venue(client)

    log.info("[4/5] 재투고 매칭 재계산")
    run_linking()

    # 3단계가 citation_count 를 갱신하면 papers.citation_percentile 이 낡는다.
    # 백분위는 상대값이라 한 논문만 고칠 수 없다 — 어떤 논문의 인용수가 바뀌면
    # 그 해 전체가 흔들린다. 이 단계가 없으면 **에러도 로그도 없이 랭킹이 옛
    # 인용도로 돈다.** 멱등이라 바뀐 게 없으면 한 행도 쓰지 않는다.
    log.info("[5/5] 인용 백분위 재계산")
    citation_percentile.refresh()


if __name__ == "__main__":
    main()
