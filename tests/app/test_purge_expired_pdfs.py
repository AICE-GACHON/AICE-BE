"""업로드 PDF 원본의 보관기간 파기 (scripts/purge_expired_pdfs.py).

개인정보처리방침이 "원본은 분석 완료 후 N일 뒤 파기한다"고 약속하고 있으므로,
이 스크립트가 도는지가 곧 그 문구가 참인지다. 여기서 지키는 것은 세 가지다:

  1. 기간이 지난 원본은 **실제로** 비워진다 (약속 이행)
  2. 아직 기간이 안 된 것과 진행 중인 분석은 건드리지 않는다 (서비스 파손 방지)
  3. 비우는 것은 pdf_bytes뿐이고 초안·분석 결과는 남는다 (사용자 기록 보존)
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.analysis import ReviewPrediction
from app.models.submission import Submission
from scripts.purge_expired_pdfs import purge_expired_pdfs
from tests.app.conftest import upload_pdf


def _age(db, submission: Submission, days: int) -> None:
    """업로드 시각을 과거로 밀어 '오래된 초안'을 만든다.

    created_at은 server_default라 INSERT 시점에 DB가 정하므로, 테스트는 만든 뒤에
    되돌려 놓는다. tz-naive로 넣는 이유는 스크립트의 cutoff 계산과 맞추기 위해서다.
    """
    submission.created_at = (datetime.now(timezone.utc).replace(tzinfo=None)
                             - timedelta(days=days))
    db.commit()


@pytest.fixture
def submission(client, auth, db) -> Submission:
    res = upload_pdf(client, auth)
    assert res.status_code == 201, res.text
    sid = res.json()["data"]["submission_id"]
    return db.get(Submission, sid)


def test_old_pdf_is_purged(client, auth, db, submission):
    _age(db, submission, days=100)

    assert purge_expired_pdfs(days=90, db=db) == 1

    db.expire_all()
    assert db.get(Submission, submission.submission_id).pdf_bytes is None


def test_recent_pdf_is_kept(client, auth, db, submission):
    _age(db, submission, days=10)

    assert purge_expired_pdfs(days=90, db=db) == 0

    db.expire_all()
    assert db.get(Submission, submission.submission_id).pdf_bytes is not None


def test_dry_run_counts_without_deleting(client, auth, db, submission):
    """--dry-run이 실제로 안 지우는지. 여기가 틀리면 운영자가 확인용으로 돌린
    명령이 그대로 파기가 된다 — 되돌릴 수 없는 종류의 사고다."""
    _age(db, submission, days=100)

    assert purge_expired_pdfs(days=90, dry_run=True, db=db) == 1

    db.expire_all()
    assert db.get(Submission, submission.submission_id).pdf_bytes is not None


def test_recent_analysis_keeps_an_old_upload(client, auth, db, submission):
    """🔴 기준 시각은 업로드가 아니라 **마지막 활동**이다.

    이 테스트가 없으면 "created_at < cutoff" 한 줄짜리 구현이 그대로 통과한다.
    그 구현은 올려두고 석 달 뒤에 처음 분석을 돌리는 사용자의 원문을, 바로 그
    분석 직후에 지운다 — 재분석하면 품질이 조용히 떨어져 있다.
    """
    _age(db, submission, days=100)
    db.add(ReviewPrediction(submission_id=submission.submission_id, status="done"))
    db.commit()

    assert purge_expired_pdfs(days=90, db=db) == 0

    db.expire_all()
    assert db.get(Submission, submission.submission_id).pdf_bytes is not None


def test_in_progress_analysis_is_never_touched(client, auth, db, submission):
    """진행 중인 분석은 요청이 끝난 뒤(BackgroundTasks) pdf_bytes를 읽는다.
    그 사이에 비우면 실행 중인 분석이 예외 없이 스텁으로 떨어진다."""
    _age(db, submission, days=100)
    prediction = ReviewPrediction(submission_id=submission.submission_id, status="running")
    db.add(prediction)
    db.commit()
    # 분석 행 자체도 오래됐다고 가정한다 — '마지막 활동' 조건으로 걸러진 것이
    # 아니라 진행 중 조건으로 걸러졌음을 분명히 하기 위해서다.
    prediction.created_at = (datetime.now(timezone.utc).replace(tzinfo=None)
                             - timedelta(days=100))
    db.commit()

    assert purge_expired_pdfs(days=90, db=db) == 0

    db.expire_all()
    assert db.get(Submission, submission.submission_id).pdf_bytes is not None


def test_purge_keeps_the_submission_and_its_analyses(client, auth, db, submission):
    """원본만 지우고 초안은 남는다. 행을 통째로 지우면 사용자가 이미 받아본
    분석 결과까지 사라진다 — 방침이 약속한 것은 원본 파기지 기록 삭제가 아니다."""
    _age(db, submission, days=100)
    db.add(ReviewPrediction(submission_id=submission.submission_id, status="done"))
    db.commit()
    db.query(ReviewPrediction).filter(
        ReviewPrediction.submission_id == submission.submission_id
    ).update({"created_at": datetime.now(timezone.utc).replace(tzinfo=None)
              - timedelta(days=100)}, synchronize_session=False)
    db.commit()

    assert purge_expired_pdfs(days=90, db=db) == 1

    db.expire_all()
    kept = db.get(Submission, submission.submission_id)
    assert kept is not None and kept.title
    assert db.query(ReviewPrediction).filter(
        ReviewPrediction.submission_id == submission.submission_id).count() == 1


def test_purged_submission_is_not_purged_again(client, auth, db, submission):
    """이미 비운 초안은 대상 집계에서 빠져야 한다. 안 그러면 매일 도는 배치가
    같은 행을 영원히 세면서 '오늘 N건 파기'라고 보고한다."""
    _age(db, submission, days=100)
    assert purge_expired_pdfs(days=90, db=db) == 1
    assert purge_expired_pdfs(days=90, db=db) == 0
