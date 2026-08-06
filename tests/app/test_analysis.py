"""분석 상태 전이 — 중복 실행 방지, 죽은 작업 회수, 소유권."""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.analysis import ReviewPrediction
from app.routers.submissions import STALE_AFTER, _now
from tests.app.conftest import upload_pdf


@pytest.fixture
def submission(client, auth):
    res = upload_pdf(client, auth)
    assert res.status_code == 201, res.text
    return res.json()["data"]["submission_id"]


def _start(client, auth, submission_id):
    return client.post(f"/api/submissions/{submission_id}/analysis",
                       headers=auth["headers"])


def test_start_returns_202_pending(client, auth, submission):
    res = _start(client, auth, submission)
    assert res.status_code == 202
    assert res.json()["data"]["status"] == "pending"
    # 백그라운드 작업이 예약됐다 (conftest가 실제 실행은 막아둔다)
    assert len(client.started_analyses) == 1


def test_start_twice_reuses_the_running_one(client, auth, submission, db):
    first = _start(client, auth, submission).json()["data"]["prediction_id"]
    second = _start(client, auth, submission).json()["data"]["prediction_id"]
    assert first == second
    # 두 번째 호출은 새 작업을 예약하지 않는다
    assert len(client.started_analyses) == 1
    assert db.query(ReviewPrediction).filter(
        ReviewPrediction.submission_id == uuid.UUID(submission)).count() == 1


def test_db_rejects_a_second_active_prediction(db, client, auth, submission):
    """부분 유니크 인덱스가 실제로 걸려 있는지 (앱 로직을 우회해서 확인).

    이게 없으면 동시 요청 두 건이 나란히 통과해 SPECTER2를 두 벌 로드한다.
    """
    _start(client, auth, submission)
    db.add(ReviewPrediction(submission_id=uuid.UUID(submission), status="running"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_finished_analysis_allows_a_new_run(client, auth, submission, db):
    """done으로 끝난 뒤에는 다시 분석할 수 있어야 한다 (인덱스는 진행 중만 막는다)."""
    first = _start(client, auth, submission).json()["data"]["prediction_id"]
    row = db.get(ReviewPrediction, uuid.UUID(first))
    row.status = "done"
    row.completed_at = _now()
    db.commit()

    second = _start(client, auth, submission).json()["data"]["prediction_id"]
    assert second != first
    assert len(client.started_analyses) == 2


def test_stale_running_analysis_is_reclaimed(client, auth, submission, db):
    """서버가 재시작되면 running 행이 남는다. 방치하면 영원히 재분석이 막힌다."""
    first = _start(client, auth, submission).json()["data"]["prediction_id"]
    row = db.get(ReviewPrediction, uuid.UUID(first))
    row.status = "running"
    row.created_at = _now() - STALE_AFTER - timedelta(minutes=1)
    db.commit()

    second = _start(client, auth, submission).json()["data"]["prediction_id"]
    assert second != first

    stale = db.get(ReviewPrediction, uuid.UUID(first))
    db.refresh(stale)
    assert stale.status == "failed"
    assert "중단" in stale.error


def test_recent_running_analysis_is_not_reclaimed(client, auth, submission, db):
    first = _start(client, auth, submission).json()["data"]["prediction_id"]
    row = db.get(ReviewPrediction, uuid.UUID(first))
    row.status = "running"
    row.created_at = _now() - STALE_AFTER + timedelta(minutes=5)
    db.commit()

    assert _start(client, auth, submission).json()["data"]["prediction_id"] == first


def test_get_before_start_is_404(client, auth, submission):
    res = client.get(f"/api/submissions/{submission}/analysis",
                     headers=auth["headers"])
    assert res.status_code == 404


def test_get_returns_status_and_empty_matches(client, auth, submission):
    _start(client, auth, submission)
    body = client.get(f"/api/submissions/{submission}/analysis",
                      headers=auth["headers"]).json()["data"]
    assert body["status"] == "pending"
    assert body["report"] is None
    assert body["matches"] == []
    assert body["explanation_source"] == "stub"


def test_analysis_of_others_submission_is_404(client, auth, other_user):
    created = upload_pdf(client, other_user).json()["data"]
    sid = created["submission_id"]
    assert _start(client, auth, sid).status_code == 404
    assert client.get(f"/api/submissions/{sid}/analysis",
                      headers=auth["headers"]).status_code == 404


def test_analysis_requires_auth(client, submission):
    assert client.post(f"/api/submissions/{submission}/analysis").status_code == 401
