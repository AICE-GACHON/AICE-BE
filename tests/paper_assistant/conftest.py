"""AI 파트 테스트 공통 준비.

노드 단위 테스트가 코퍼스 통계(venue_stats / aspect_base_rates)를 **DB에서** 읽지
않게 막는다. 두 가지 이유:

1. 느리다 — DB가 내려가 있으면 조회마다 커넥션 타임아웃을 기다린다.
2. 결정론적이지 않다 — 실제 통계가 있으면 rating 맥락이 붙고, 없으면 안 붙는다.
   같은 테스트가 환경에 따라 다른 것을 검증하게 된다.

실제 DB가 필요한 테스트(test_db_integration)는 이 노드들을 쓰지 않으므로 영향받지
않는다. 통계가 붙은 상태를 보고 싶은 테스트는 이 fixture를 덮어쓰면 된다.
"""
import pytest

from paper_assistant.graph import nodes


@pytest.fixture(autouse=True)
def _no_corpus_stats(monkeypatch):
    monkeypatch.setattr(nodes, "load_venue_stats", lambda *a, **k: {})
    monkeypatch.setattr(nodes, "load_base_rates", lambda *a, **k: {})
