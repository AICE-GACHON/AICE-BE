"""arXiv/S2 보강 전체 실행 — 하베스트 → arXiv 매칭 → S2 보강 → 재투고 재계산.

각 단계가 멱등이라 중간에 끊겨도 그냥 다시 실행하면 이어진다.

    python scripts/run_enrichment.py                  # 전 단계
    python scripts/run_enrichment.py --skip-harvest   # 캐시가 이미 있을 때
    python scripts/run_enrichment.py --citations      # 인용 엣지까지

소요 시간(실측 기준): 하베스트가 압도적으로 길다(cs+stat 2018년 이후 ≈ 2~3시간,
1요청 1,300건/16초). 나머지는 합쳐서 10~20분.
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paper_assistant.ingest import arxiv_client, arxiv_matcher, s2_enricher  # noqa: E402
from paper_assistant.ingest.s2_client import S2Client  # noqa: E402
from paper_assistant.ingest.submission_linker import run_linking  # noqa: E402

log = logging.getLogger("enrich-all")


def main() -> None:
    ap = argparse.ArgumentParser(description="arXiv/S2 보강 일괄 실행")
    ap.add_argument("--skip-harvest", action="store_true",
                    help="arXiv 하베스트를 건너뛴다 (캐시 재사용)")
    ap.add_argument("--citations", action="store_true",
                    help="S2 references로 코퍼스 내부 인용 엣지도 적재")
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

    log.info("[3/4] Semantic Scholar 보강")
    client = S2Client()
    s2_enricher.enrich_by_arxiv(client)
    s2_enricher.enrich_by_venue(client)
    if args.citations:
        s2_enricher.enrich_citations(client)

    log.info("[4/4] 재투고 매칭 재계산")
    run_linking()


if __name__ == "__main__":
    main()
