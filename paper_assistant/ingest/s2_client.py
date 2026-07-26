"""Semantic Scholar Graph API 클라이언트 (설계 §5).

실측으로 확인된 API 특성 (2026-07 기준):
- **API 키가 사실상 필수**다. 키 없이 보내면 어떤 엔드포인트든 즉시 429가 온다
  (익명 풀이 전역 공유라 항상 포화). 키가 있으면 200.
- `POST /paper/batch` : 1요청 500 id까지. 입력 순서대로 결과가 오고 못 찾은 id는
  **null**로 채워진다. 대량 보강의 주력 엔드포인트.
- `GET /paper/search/match` : 제목 1건 = 요청 1건. 키가 있어도 429가 잦아
  대량 매칭에는 쓸 수 없다 → 제목 매칭은 arXiv 로컬 캐시로 한다(arxiv_matcher).
- `GET /paper/search/bulk` : 1페이지 1,000건 + token 페이지네이션. query 없이
  venue/year 필터만으로도 동작한다. 간헐적으로 500을 뱉으므로 재시도가 필수.
- id 접두어: ARXIV: / DOI: / CorpusId: / MAG: / ACL: / PMID: / URL:
"""
import logging
import re
import time

import requests

from paper_assistant import config

BASE = "https://api.semanticscholar.org/graph/v1"
BATCH_SIZE = 500       # /paper/batch 상한
BULK_PAGE = 1000       # /paper/search/bulk 1페이지
MAX_RETRIES = 10
GAP = 1.1              # /paper/batch 요청 간 최소 간격(초)
# /paper/search/* 는 batch보다 훨씬 빡빡하다. gap 1.1초로 토큰 페이지네이션을 돌리면
# 6페이지쯤에서 429가 연속으로 나 재시도를 다 태우고 죽는다(실측) → 넉넉히 벌린다.
SEARCH_GAP = 4.0
MAX_BACKOFF = 60

log = logging.getLogger("s2")


class S2Client:
    """레이트리밋/일시적 5xx를 흡수하는 얇은 래퍼."""

    def __init__(self, api_key: str | None = None, gap: float = GAP,
                 search_gap: float = SEARCH_GAP):
        key = api_key if api_key is not None else config.S2_API_KEY
        if not key:
            raise RuntimeError(
                "S2_API_KEY가 없습니다. 키 없이는 모든 요청이 429입니다. "
                "https://www.semanticscholar.org/product/api#api-key 에서 발급해 "
                ".env에 S2_API_KEY로 넣으세요 (.env.example 참고).")
        self.session = requests.Session()
        self.session.headers.update({
            "x-api-key": key,
            "User-Agent": "paper-assistant/0.1 (academic project)"})
        self.gap = gap
        self.search_gap = search_gap
        self._last = 0.0

    def _throttle(self, gap: float) -> None:
        wait = gap - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _request(self, method: str, path: str, **kwargs):
        gap = self.search_gap if path.startswith("/paper/search") else self.gap
        for attempt in range(MAX_RETRIES):
            self._throttle(gap)
            r = self.session.request(method, f"{BASE}{path}", timeout=180, **kwargs)
            # 429는 레이트리밋, 500/504는 S2 쪽 일시 장애 — 둘 다 재시도로 넘긴다.
            if r.status_code in (429, 500, 502, 503, 504):
                wait = min(2 ** attempt, MAX_BACKOFF)
                # 1~2회는 흔하다. 그 이상 반복되면 눈에 보이게 올린다.
                emit = log.warning if attempt >= 2 else log.debug
                emit("%d %s — %ds 후 재시도(%d/%d)",
                     r.status_code, path, wait, attempt + 1, MAX_RETRIES)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"{path}: {MAX_RETRIES}회 재시도 후에도 실패 "
                           f"(마지막 {r.status_code})")

    # ------------------------------------------------------------ 엔드포인트

    def paper_batch(self, ids: list[str], fields: str) -> list[dict | None]:
        """id 목록 → 논문 메타데이터. 결과는 입력 순서, 미발견은 None."""
        out: list[dict | None] = []
        for i in range(0, len(ids), BATCH_SIZE):
            chunk = ids[i:i + BATCH_SIZE]
            data = self._request("POST", "/paper/batch", params={"fields": fields},
                                 json={"ids": chunk})
            if not isinstance(data, list) or len(data) != len(chunk):
                raise RuntimeError(f"/paper/batch 응답 길이 불일치: "
                                   f"{len(data) if isinstance(data, list) else data}")
            out.extend(data)
            log.info("batch %d/%d", min(i + BATCH_SIZE, len(ids)), len(ids))
        return out

    def search_bulk_pages(self, fields: str, **filters):
        """venue/year 등 필터로 대량 조회. **페이지 단위**로 yield한다.

        호출부가 페이지마다 결과를 반영할 수 있게 페이지로 끊어 준다 — 깊은 페이지에서
        레이트리밋에 막혀도 그때까지 받은 분량은 살린다(실측: 6페이지째에서 429 연속).
        """
        params = {"fields": fields, **filters}
        token = None
        fetched = 0
        while True:
            if token:
                params["token"] = token
            data = self._request("GET", "/paper/search/bulk", params=params)
            rows = data.get("data") or []
            fetched += len(rows)
            log.info("bulk %s%s: %d/%s",
                     filters.get("venue", ""), filters.get("year", ""),
                     fetched, data.get("total"))
            if rows:
                yield rows
            token = data.get("token")
            if not token or not rows:
                return

    def search_bulk(self, fields: str, **filters):
        """search_bulk_pages를 평탄화 — 논문 dict를 하나씩 yield."""
        for page in self.search_bulk_pages(fields, **filters):
            yield from page


def arxiv_ref(arxiv_id: str) -> str:
    """'2401.01234v3' → 'ARXIV:2401.01234' (버전 접미사만 제거).

    구형 id('solv-int/9901001v1')에 'v'가 들어 있어 단순 split('v')는 쓸 수 없다.
    """
    return "ARXIV:" + strip_version(arxiv_id)


def strip_version(arxiv_id: str) -> str:
    return re.sub(r"v\d+$", "", (arxiv_id or "").strip())
