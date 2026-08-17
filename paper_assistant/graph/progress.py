"""진행 상황을 파이프라인 밖으로 흘려보내는 통로.

노드는 `emit(...)`만 부르면 되고, 그 값이 어디로 가는지는 모른다. 실제 배선은
LangGraph의 custom 스트림이고, 그것을 받아 호출자의 on_event로 넘기는 것은
pipeline.analyze()의 몫이다.

**그래프 밖에서 불러도 안전해야 한다.** 노드 함수를 직접 부르는 단위 테스트가
여럿 있고(tests/paper_assistant/test_pipeline_structure.py), 그때
get_stream_writer()는 RuntimeError를 낸다("Called get_config outside of a
runnable context" — langgraph 1.2.10 실측). 진행 표시 하나 때문에 노드가
단독으로 못 돌게 되면 그 테스트들이 전부 그래프를 띄워야 한다. 그래서 여기서
삼키고 조용히 no-op이 된다.

같은 이유로 **emit은 절대 예외를 밖으로 내지 않는다.** 진행 표시가 실패해서
분석이 죽는 것은 앞뒤가 바뀐 일이다.
"""
import logging
from datetime import datetime, timezone

from paper_assistant.schemas import ProgressEvent

log = logging.getLogger(__name__)

# 단계 id와 등장 순서. 화면이 "아직 오지 않은 단계"를 미리 회색으로 그릴 수 있게
# 목록으로 공개한다. 실제로 안 오는 단계도 있다 — extract는 업로드 때 제목·초록을
# 이미 뽑아둔 경우 건너뛰고, review_fetch는 고른 논문이 없으면 할 일이 없다.
STEPS = ("prepare", "extract", "retrieval", "rerank", "review_fetch", "synthesis")


def event(step: str, label: str, *, done: bool = False,
          detail: str | None = None) -> ProgressEvent:
    """이벤트 1건을 만든다 (시각을 여기서 찍는다).

    그래프 **밖**에서 진행을 알려야 하는 곳이 있어 emit과 분리해 뒀다 — 첫 분석의
    SPECTER2 로드(수십 초)는 노드가 아니라 그래프를 만드는 단계에서 일어나고,
    하필 그 구간이 사용자가 가장 오래 기다리는 침묵이다.
    """
    return ProgressEvent(
        step=step, done=done, label=label, detail=detail,
        at=datetime.now(timezone.utc).isoformat())


def emit(step: str, label: str, *, done: bool = False,
         detail: str | None = None) -> None:
    """노드 안에서 진행 이벤트를 내보낸다. 스트림이 없으면 조용히 아무것도 안 한다."""
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()(event(step, label, done=done, detail=detail))
    except RuntimeError:
        # 그래프 밖에서 노드를 직접 부른 경우 (단위 테스트 등). 정상이다.
        pass
    except Exception:
        # 그 밖의 사고는 원인을 남기되 분석은 계속한다.
        log.exception("진행 이벤트를 내보내지 못했습니다 (step=%s)", step)
