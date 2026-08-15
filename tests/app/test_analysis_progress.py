"""분석 진행 상황(progress) — 쌓이는가, 순서가 맞는가, 폴링으로 나오는가.

파이프라인이 내보내는 ProgressEvent를 백엔드가 어떻게 보관하고 돌려주는지만 본다.
어떤 단계에서 무슨 문구가 나오는지는 AI 파트의 몫이다
(tests/paper_assistant/test_progress.py).
"""
import uuid

import pytest

from app.database import SessionLocal
from app.models.analysis import ReviewPrediction
from app.services import analysis as analysis_service
from paper_assistant.schemas import ProgressEvent
from tests.app.conftest import upload_pdf


@pytest.fixture
def submission(client, auth):
    res = upload_pdf(client, auth)
    assert res.status_code == 201, res.text
    return res.json()["data"]["submission_id"]


@pytest.fixture
def recorder(db, monkeypatch):
    """_progress_recorder가 테스트 트랜잭션 안에서 돌게 묶어 둔다.

    운영에서는 **일부러 분석과 다른 세션**을 쓴다 — 진행 기록의 사고가 분석
    트랜잭션을 오염시키지 않게 하려는 것이다(app/services/analysis.py). 그런데 그
    새 세션은 풀에서 다른 커넥션을 받으므로, 테스트가 아직 커밋하지 않은 행을 보지
    못하고 UPDATE가 0행에 걸린다. 그래서 여기서만 같은 커넥션에 묶는다.
    """
    bind = db.get_bind()
    monkeypatch.setattr(
        analysis_service, "SessionLocal",
        lambda: SessionLocal(bind=bind, join_transaction_mode="create_savepoint"))
    return analysis_service._progress_recorder


def _event(step, label, done=False, detail=None, at="2026-08-15T00:00:00+00:00"):
    return ProgressEvent(step=step, done=done, label=label, detail=detail, at=at)


def _start(client, auth, submission_id):
    return client.post(f"/api/submissions/{submission_id}/analysis",
                       headers=auth["headers"])


def _poll(client, auth, submission_id):
    res = client.get(f"/api/submissions/{submission_id}/analysis",
                     headers=auth["headers"])
    assert res.status_code == 200, res.text
    return res.json()["data"]


# ------------------------------------------------------------------ 기본형

def test_progress_starts_empty(client, auth, submission):
    """NULL이 아니라 빈 배열이어야 한다 — 프론트에 None 분기를 만들지 않는다."""
    _start(client, auth, submission)
    assert _poll(client, auth, submission)["progress"] == []


def test_events_are_appended_in_order(client, auth, submission, db, recorder):
    prediction_id = uuid.UUID(_start(client, auth, submission).json()["data"]["prediction_id"])
    record = recorder(prediction_id)

    record(_event("prepare", "분석을 준비하고 있어요"))
    record(_event("prepare", "준비를 마쳤어요", done=True))
    record(_event("retrieval", "비슷한 논문을 찾고 있어요"))

    progress = _poll(client, auth, submission)["progress"]
    assert [(e["step"], e["done"]) for e in progress] == [
        ("prepare", False), ("prepare", True), ("retrieval", False)]
    assert progress[0]["label"] == "분석을 준비하고 있어요"


def test_detail_survives_the_round_trip(client, auth, submission, recorder):
    prediction_id = uuid.UUID(_start(client, auth, submission).json()["data"]["prediction_id"])
    recorder(prediction_id)(
        _event("retrieval", "후보 50편을 찾았어요", done=True, detail="믿기 어려워요"))

    [event] = _poll(client, auth, submission)["progress"]
    assert event["detail"] == "믿기 어려워요"


# --------------------------------------------------- 두 세션이 같은 행을 쓴다

def test_analysis_commit_does_not_wipe_progress(client, auth, submission, db, recorder):
    """**이 설계의 핵심 위험이다.**

    진행은 별도 세션이 쓰고, 분석 결과는 원래 세션이 쓴다. 결과를 저장할 때 행
    전체를 덮어쓰면 그 사이 쌓인 진행이 통째로 사라진다 — 사용자에게는 분석이
    끝나는 순간 진행 기록이 증발하는 것으로 보인다.
    """
    prediction_id = uuid.UUID(_start(client, auth, submission).json()["data"]["prediction_id"])
    record = recorder(prediction_id)
    record(_event("retrieval", "찾는 중"))

    # run_analysis가 마지막에 하는 일을 흉내낸다 (다른 세션에서 결과 컬럼만 수정).
    prediction = db.get(ReviewPrediction, prediction_id)
    prediction.status = "done"
    prediction.explanation_source = "llm"
    db.commit()

    data = _poll(client, auth, submission)
    assert data["status"] == "done"
    assert [e["step"] for e in data["progress"]] == ["retrieval"]


def test_progress_written_after_a_stale_read_still_lands(client, auth, submission,
                                                         db, recorder):
    """분석 세션이 행을 읽어둔 뒤에 쓴 진행도 남아야 한다 (읽고-고쳐-쓰기였다면
    마지막 쓰기가 앞의 것을 덮어쓴다)."""
    prediction_id = uuid.UUID(_start(client, auth, submission).json()["data"]["prediction_id"])
    record = recorder(prediction_id)

    db.get(ReviewPrediction, prediction_id)      # 이 시점 progress = []
    record(_event("prepare", "1"))
    record(_event("retrieval", "2"))

    assert [e["label"] for e in _poll(client, auth, submission)["progress"]] == ["1", "2"]


# ------------------------------------------------------------------ 사고 처리

def test_recording_onto_a_deleted_prediction_is_harmless(client, auth, submission,
                                                         db, recorder):
    """분석 도중 사용자가 초안을 지울 수 있다. 그때 진행 기록이 예외를 내면
    (analyze가 삼키긴 하지만) 로그가 이벤트마다 스택트레이스로 뒤덮인다."""
    prediction_id = uuid.UUID(_start(client, auth, submission).json()["data"]["prediction_id"])
    record = recorder(prediction_id)

    client.delete(f"/api/submissions/{submission}", headers=auth["headers"])
    record(_event("retrieval", "이제 갈 곳이 없는 이벤트"))     # 0행 UPDATE — 조용해야 한다


def test_a_new_analysis_starts_with_a_clean_slate(client, auth, submission, db, recorder):
    """재시도는 새 prediction 행이라 이전 진행이 섞여 보이면 안 된다.

    폴링(_poll)이 아니라 행을 직접 본다. created_at의 server_default가 now()이고
    postgres의 now()는 **트랜잭션 시작 시각**이라, 테스트 전체가 한 트랜잭션인
    여기서는 두 행의 created_at이 같은 값이 된다 — latest_prediction의 정렬이
    둘 중 무엇을 집을지 정해지지 않는다. 실제 서비스에서는 두 분석이 서로 다른
    트랜잭션(보통 몇 분 차이)이라 생기지 않는 모호함이다.
    """
    first = uuid.UUID(_start(client, auth, submission).json()["data"]["prediction_id"])
    recorder(first)(_event("retrieval", "첫 번째 분석의 흔적"))

    row = db.get(ReviewPrediction, first)
    row.status = "done"
    row.completed_at = analysis_service._now()
    db.commit()

    second = uuid.UUID(_start(client, auth, submission).json()["data"]["prediction_id"])
    assert second != first
    assert db.get(ReviewPrediction, second).progress == []
    # 첫 번째 것은 그대로 남아 있다 (덮어쓰기가 아니라 새 행이다).
    assert len(db.get(ReviewPrediction, first).progress) == 1
