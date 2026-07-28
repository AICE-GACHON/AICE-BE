"""arXiv 메타데이터 수집 — OAI-PMH 대량 하베스트 + 단건 제목 검색 (설계 §5).

왜 대량 하베스트인가 (실측 결과):
- S2 `/paper/search/match`는 제목 1건 = 요청 1건인데 API 키가 있어도 429가 잦다.
  43,515편을 이 엔드포인트로 매칭하는 것은 현실적으로 불가능하다.
- arXiv OAI-PMH는 1요청에 1,300건(≈15초, 약 87건/초). cs+stat 전체 메타데이터를
  한 번 받아 로컬 제목 인덱스를 만드는 편이 압도적으로 빠르고 예측 가능하다.

실측으로 확인된 OAI-PMH 특성:
- `from`은 제출일이 아니라 datestamp(최종 수정일) 기준이다 → 오래된 논문도 수정만
  있었으면 들어온다. 커버리지에는 유리하고 누락 위험은 없다.
- resumptionToken에 `from=...&skip=N`이 들어있다 → 토큰만 저장하면 중단 후 이어받기.
- 과부하 시 503 + Retry-After. 병렬 하베스트는 금지되어 있어 순차로만 받는다.
- header에 status="deleted"인 레코드는 metadata가 없다 → 건너뛴다.

캐시 (data/raw/):
    arxiv_meta.jsonl.gz     레코드 1줄당 {id, title, created, updated, cats, keynames}
    arxiv_harvest.json      set별 resumptionToken 체크포인트

사용법:
    python -m paper_assistant.ingest.arxiv_client              # 이어서 하베스트
    python -m paper_assistant.ingest.arxiv_client --from 2018-01-01
"""
import argparse
import gzip
import json
import logging
import re
import time
import xml.etree.ElementTree as ET

import requests

from paper_assistant import config
from paper_assistant.ingest._http import new_session, request_with_retry

OAI_URL = "http://export.arxiv.org/oai2"
ATOM_URL = "http://export.arxiv.org/api/query"
# ML 논문의 arXiv 카테고리는 대부분 cs.* 또는 stat.ML 이다.
DEFAULT_SETS = ("cs", "stat")
# ICLR 2020 투고(2019-09)보다 앞선 프리프린트도 잡히도록 여유를 둔다.
DEFAULT_FROM = "2018-01-01"

REQUEST_GAP = 3.0      # arXiv 권장 간격 (응답 자체가 ≈15초라 사실상 여유)
MAX_RETRIES = 6

NS = {"oai": "http://www.openarchives.org/OAI/2.0/",
      "arxiv": "http://arxiv.org/OAI/arXiv/",
      "atom": "http://www.w3.org/2005/Atom"}

CACHE_PATH = config.RAW_DIR / "arxiv_meta.jsonl.gz"
CHECKPOINT_PATH = config.RAW_DIR / "arxiv_harvest.json"

log = logging.getLogger("arxiv")


# --------------------------------------------------------------- HTTP

def _session() -> requests.Session:
    return new_session()


def _oai_decide(r, attempt: int) -> float | None:
    """503(Retry-After)과 일시적 5xx를 흡수한다."""
    if r.status_code == 503:
        wait = int(r.headers.get("Retry-After") or 20)
        log.warning("503 과부하 — %ds 대기", wait)
        return wait
    if r.status_code >= 500:
        wait = 10 * (attempt + 1)
        log.warning("%d — %ds 후 재시도", r.status_code, wait)
        return wait
    return None


def _oai_get(session: requests.Session, params: dict) -> ET.Element:
    """OAI-PMH 1요청 → 응답 XML 루트."""
    r = request_with_retry(session, "GET", OAI_URL, decide=_oai_decide,
                           retries=MAX_RETRIES, timeout=300, label="OAI",
                           params=params)
    root = ET.fromstring(r.content)
    err = root.find("oai:error", NS)
    if err is not None:
        code = err.get("code")
        # noRecordsMatch: 조건에 맞는 레코드 없음 → 정상 종료로 취급
        if code != "noRecordsMatch":
            raise RuntimeError(f"OAI 오류 {code}: {err.text}")
    return root


# --------------------------------------------------------------- 파싱

def parse_record(record: ET.Element) -> dict | None:
    """OAI record 1건 → 캐시 레코드. 삭제된 레코드는 None."""
    header = record.find("oai:header", NS)
    if header is not None and header.get("status") == "deleted":
        return None
    meta = record.find("oai:metadata/arxiv:arXiv", NS)
    if meta is None:
        return None

    def text(tag: str) -> str:
        el = meta.find(f"arxiv:{tag}", NS)
        return (el.text or "").strip() if el is not None else ""

    title = " ".join(text("title").split())
    arxiv_id = text("id")
    if not title or not arxiv_id:
        return None

    keynames = [(kn.text or "").strip()
                for kn in meta.findall("arxiv:authors/arxiv:author/arxiv:keyname", NS)]
    return {"id": arxiv_id,
            "title": title,
            "created": text("created"),
            "updated": text("updated"),
            "cats": text("categories").split(),
            "keynames": [k for k in keynames if k]}


# --------------------------------------------------------------- 하베스트

def _load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    return {}


def _save_checkpoint(state: dict) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                               encoding="utf-8")


def harvest(sets: tuple[str, ...] = DEFAULT_SETS, from_date: str = DEFAULT_FROM,
            max_pages: int | None = None) -> int:
    """set별 OAI-PMH 하베스트. 캐시에 append하고 토큰을 체크포인트에 남긴다.

    같은 논문이 여러 번 append될 수 있다(cs와 stat 양쪽 set, 재실행). 인덱스를
    만들 때 arXiv id로 덮어쓰므로 중복은 무해하다.
    """
    state = _load_checkpoint()
    session = _session()
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    total = 0

    for set_name in sets:
        st = state.setdefault(set_name, {"token": None, "records": 0, "done": False})
        if st["done"]:
            log.info("set=%s 이미 완료 (%d건) — 건너뜀", set_name, st["records"])
            continue

        pages = 0
        while True:
            if st["token"]:
                params = {"verb": "ListRecords", "resumptionToken": st["token"]}
            else:
                params = {"verb": "ListRecords", "set": set_name,
                          "metadataPrefix": "arXiv", "from": from_date}

            t0 = time.time()
            root = _oai_get(session, params)
            records = root.findall("oai:ListRecords/oai:record", NS)
            rows = [r for r in (parse_record(rec) for rec in records) if r]

            with gzip.open(CACHE_PATH, "at", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

            st["records"] += len(rows)
            total += len(rows)
            token_el = root.find("oai:ListRecords/oai:resumptionToken", NS)
            st["token"] = (token_el.text or "").strip() if token_el is not None else ""
            if not st["token"]:
                st["done"] = True
            _save_checkpoint(state)

            pages += 1
            log.info("set=%s page=%d +%d건 (누적 %d) %.1fs",
                     set_name, pages, len(rows), st["records"], time.time() - t0)
            if st["done"]:
                log.info("set=%s 하베스트 완료: %d건", set_name, st["records"])
                break
            if max_pages and pages >= max_pages:
                log.info("set=%s max_pages(%d) 도달 — 토큰 저장 후 중단", set_name, max_pages)
                break
            time.sleep(REQUEST_GAP)

    return total


def iter_cached(path=CACHE_PATH):
    """캐시된 arXiv 레코드를 하나씩 yield."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} 없음 — 먼저 `python -m paper_assistant.ingest.arxiv_client` 실행")
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def cache_status() -> dict:
    """체크포인트 요약 (진행 상황 확인용)."""
    return _load_checkpoint()


# --------------------------------------------------------------- 단건 검색

def search_by_title(title: str, session: requests.Session | None = None) -> dict | None:
    """Atom API로 제목 1건 조회. 하베스트에서 놓친 논문의 폴백/검증용.

    1요청 = 1편이고 arXiv가 3초 간격을 권고하므로 대량 사용은 금지.
    """
    session = session or _session()
    quoted = title.replace('"', " ").strip()
    params = {"search_query": f'ti:"{quoted}"', "max_results": 5}
    for attempt in range(3):
        r = session.get(ATOM_URL, params=params, timeout=60)
        if r.status_code >= 500 or r.status_code == 503:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for entry in root.findall("atom:entry", NS):
            got = " ".join((entry.findtext("atom:title", "", NS) or "").split())
            raw_id = entry.findtext("atom:id", "", NS) or ""
            # http://arxiv.org/abs/2401.01234v2 → 2401.01234
            # 구형 id는 'abs/solv-int/9901001v1' 형태라 archive 부분을 남겨야 한다.
            arxiv_id = re.sub(r"v\d+$", "", raw_id.split("/abs/")[-1])
            return {"id": arxiv_id, "title": got}
        return None
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="arXiv OAI-PMH 하베스트")
    ap.add_argument("--from", dest="from_date", default=DEFAULT_FROM,
                    help=f"datestamp 하한 (기본 {DEFAULT_FROM})")
    ap.add_argument("--sets", default=",".join(DEFAULT_SETS),
                    help=f"OAI set 목록 (기본 {','.join(DEFAULT_SETS)})")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="set당 최대 페이지 (시험용)")
    ap.add_argument("--status", action="store_true", help="진행 상황만 출력")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.status:
        print(json.dumps(cache_status(), ensure_ascii=False, indent=2))
        return
    n = harvest(tuple(s.strip() for s in args.sets.split(",") if s.strip()),
                args.from_date, args.max_pages)
    log.info("이번 실행에서 %d건 추가 → %s", n, CACHE_PATH)


if __name__ == "__main__":
    main()
