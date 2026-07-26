"""arXiv 매칭 순수 로직 테스트 (네트워크/DB 불필요)."""
import xml.etree.ElementTree as ET

from paper_assistant.ingest.arxiv_client import parse_record
from paper_assistant.ingest.arxiv_matcher import (
    build_index, choose, match_papers, surname)

TITLE = "Attention Is All You Need Again For Long Sequences"


def _rec(arxiv_id, title=TITLE, keynames=("Vaswani",), created="2019-06-01"):
    return {"id": arxiv_id, "title": title, "created": created, "updated": created,
            "cats": ["cs.LG"], "keynames": list(keynames)}


def _paper(pid=1, title=TITLE, year=2020, surnames=("vaswani",)):
    return {"id": pid, "title": title, "year": year, "surnames": set(surnames)}


# ------------------------------------------------------------------- surname

def test_surname_takes_last_token():
    assert surname("Alice B. Kim") == "kim"
    assert surname("Jean-Luc Picard") == "picard"
    assert surname("") == ""


# --------------------------------------------------------------------- 인덱스

def test_build_index_dedupes_same_arxiv_id_and_skips_short_titles():
    index = build_index([_rec("2001.00001"), _rec("2001.00001"),
                         _rec("2001.00002", title="Short Title")])
    assert len(index["attention is all you need again for long sequences"]) == 1
    # 짧은 제목은 일반 명사구 충돌 위험이 커 인덱스에 넣지 않는다
    assert all("short title" not in k for k in index)


# ---------------------------------------------------------------------- 선택

def test_choose_requires_author_overlap():
    assert choose(_paper(), [_rec("2001.1")]) is not None
    # 제목은 같지만 저자가 전혀 겹치지 않으면 동명 논문으로 보고 버린다
    assert choose(_paper(surnames=("kim",)), [_rec("2001.1")]) is None


def test_choose_accepts_when_one_side_has_no_authors():
    assert choose(_paper(surnames=()), [_rec("2001.1")]) is not None
    assert choose(_paper(), [_rec("2001.1", keynames=())]) is not None


def test_choose_rejects_arxiv_posted_long_after_submission():
    late = _rec("2401.1", created="2024-01-01")
    assert choose(_paper(year=2020), [late]) is None


def test_choose_prefers_better_author_overlap():
    a = _rec("2001.1", keynames=("Vaswani",))
    b = _rec("2001.2", keynames=("Vaswani", "Shazeer"))
    got = choose(_paper(surnames=("vaswani", "shazeer")), [a, b])
    assert got["id"] == "2001.2"


def test_choose_gives_up_on_tie():
    a = _rec("2001.1", keynames=("Vaswani",))
    b = _rec("2001.2", keynames=("Vaswani",))
    assert choose(_paper(), [a, b]) is None


# ---------------------------------------------------------------- match_papers

def test_match_papers_reports_stats():
    index = build_index([_rec("2001.1")])
    updates, stats = match_papers(
        [_paper(1),                                   # 매칭
         _paper(2, title="Tiny"),                     # 제목 짧음
         _paper(3, title="Some Completely Unrelated Paper Title Here")],  # 미발견
        index)
    assert updates == [("2001.1", 1)]
    assert stats == {"papers": 3, "title_hit": 1, "matched": 1,
                     "short_title": 1, "ambiguous": 0}


# ------------------------------------------------------------- OAI 레코드 파싱

OAI_RECORD = """
<record xmlns="http://www.openarchives.org/OAI/2.0/">
  <header><identifier>oai:arXiv.org:2001.00001</identifier><datestamp>2020-01-05</datestamp></header>
  <metadata>
    <arXiv xmlns="http://arxiv.org/OAI/arXiv/">
      <id>2001.00001</id>
      <created>2020-01-02</created>
      <updated>2020-03-04</updated>
      <authors>
        <author><keyname>Kim</keyname><forenames>Alice</forenames></author>
        <author><keyname>Lee</keyname><forenames>Bob</forenames></author>
      </authors>
      <title>A Study of
      Wrapped Titles</title>
      <categories>cs.LG stat.ML</categories>
    </arXiv>
  </metadata>
</record>
"""

DELETED_RECORD = """
<record xmlns="http://www.openarchives.org/OAI/2.0/">
  <header status="deleted"><identifier>oai:arXiv.org:9901.00001</identifier></header>
</record>
"""


def test_parse_record_flattens_wrapped_title():
    rec = parse_record(ET.fromstring(OAI_RECORD))
    assert rec["id"] == "2001.00001"
    assert rec["title"] == "A Study of Wrapped Titles"
    assert rec["keynames"] == ["Kim", "Lee"]
    assert rec["cats"] == ["cs.LG", "stat.ML"]
    assert rec["created"] == "2020-01-02"


def test_parse_record_skips_deleted():
    assert parse_record(ET.fromstring(DELETED_RECORD)) is None
