"""분석 결과의 공개 공유 링크 (이슈 #30).

이 기능은 **인증 벽에 의도적으로 구멍을 내는 것**이라, 여기서 지키는 것은 편의가
아니라 경계다:

  1. 구멍은 소유자가 연 것만 열린다 (남이 발급할 수 없고, 폐기하면 닫힌다)
  2. 구멍으로 나가는 것은 정해진 것뿐이다 (개인정보·원문 PDF·내부 식별자 제외)
  3. 구멍의 자물쇠는 토큰 하나다 (추측 불가능해야 하고, 사유를 구분해 알려주지 않는다)

특히 2번은 스키마에 필드를 하나 추가하는 것으로 조용히 깨진다. 그래서 응답 키를
**정확히 일치**로 검사한다 — 필드가 늘면 테스트가 먼저 실패하고, 그때 "이게 공개돼도
되는가"를 한 번 더 묻게 된다.
"""
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.analysis import ReviewPrediction
from app.models.share import SubmissionShare
from app.models.submission import Submission
from tests.app.conftest import upload_pdf

# 공개 응답에 실려도 되는 것의 전부. 늘리려면 SharedAnalysisResponse의 docstring을
# 먼저 읽을 것.
PUBLIC_FIELDS = {"title", "abstract", "field", "report"}

_REPORT = {
    "query_title": "Graph Neural Networks for Molecules",
    "query_abstract": "We propose a message-passing GNN for molecules.",
    "summary_markdown": "요약",
}


@pytest.fixture
def submission_id(client, auth) -> str:
    res = upload_pdf(client, auth)
    assert res.status_code == 201, res.text
    return res.json()["data"]["submission_id"]


def _finish_analysis(db, submission_id: str, report=None) -> ReviewPrediction:
    """분석이 끝난 상태를 만든다. 실제 실행은 conftest가 막아 두었다."""
    prediction = ReviewPrediction(
        submission_id=uuid.UUID(submission_id), status="done",
        report=_REPORT if report is None else report)
    db.add(prediction)
    db.commit()
    return prediction


@pytest.fixture
def shared(client, auth, db, submission_id) -> dict:
    """분석까지 끝내고 공유 링크를 발급한 상태."""
    _finish_analysis(db, submission_id)
    res = client.post(f"/api/submissions/{submission_id}/share", headers=auth["headers"])
    assert res.status_code == 200, res.text
    return {"submission_id": submission_id, **res.json()["data"]}


# ------------------------------------------------------------------ 발급

def test_owner_can_issue_a_share_link(shared):
    assert shared["token"]
    # 서버가 프론트 공개 라우트로 조립해 준다 (프론트가 다시 만들지 않도록).
    assert shared["url"].endswith(f"/shared/{shared['token']}")


def test_token_is_unguessable(client, auth, db):
    """토큰이 이 링크의 유일한 자물쇠다. 짧거나 예측 가능하면 공개 여부를 소유자가
    아니라 대입 시도자가 정하게 된다."""
    tokens = set()
    for _ in range(3):
        sid = upload_pdf(client, auth).json()["data"]["submission_id"]
        _finish_analysis(db, sid)
        res = client.post(f"/api/submissions/{sid}/share", headers=auth["headers"])
        tokens.add(res.json()["data"]["token"])

    assert len(tokens) == 3                      # 재사용·순번이 아니다
    # secrets.token_urlsafe(32) = base64url 43자. 넉넉히 40자 이상만 확인한다.
    assert all(len(t) >= 40 for t in tokens)
    assert all(set(t) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        for t in tokens)


def test_issuing_twice_returns_the_same_link(client, auth, db, submission_id):
    """공유 버튼을 두 번 눌렀다는 이유로 먼저 보낸 링크가 죽으면 안 된다."""
    _finish_analysis(db, submission_id)
    first = client.post(f"/api/submissions/{submission_id}/share",
                        headers=auth["headers"]).json()["data"]["token"]
    second = client.post(f"/api/submissions/{submission_id}/share",
                         headers=auth["headers"]).json()["data"]["token"]
    assert first == second


def test_cannot_share_before_analysis_is_done(client, auth, submission_id):
    """결과가 없는 링크를 만들면 받는 사람은 빈 화면을 본다."""
    res = client.post(f"/api/submissions/{submission_id}/share", headers=auth["headers"])
    assert res.status_code == 409, res.text


def test_failed_analysis_cannot_be_shared(client, auth, db, submission_id):
    db.add(ReviewPrediction(submission_id=uuid.UUID(submission_id), status="failed",
                            error="분석에 실패했습니다."))
    db.commit()
    res = client.post(f"/api/submissions/{submission_id}/share", headers=auth["headers"])
    assert res.status_code == 409, res.text


def test_stranger_cannot_issue_a_share_link(client, auth, other_user, db, submission_id):
    """남의 초안을 공개해 버리는 것은 이 기능에서 가장 나쁜 실패다.

    404인 것은 이 저장소의 규칙이다 — 403은 "그 UUID의 초안이 실재한다"를 알려준다
    (app/services/submissions.py owned_submission).
    """
    _finish_analysis(db, submission_id)
    res = client.post(f"/api/submissions/{submission_id}/share",
                      headers=other_user["headers"])
    assert res.status_code == 404, res.text
    assert db.query(SubmissionShare).count() == 0


def test_anonymous_cannot_issue_a_share_link(client, db, submission_id):
    _finish_analysis(db, submission_id)
    assert client.post(f"/api/submissions/{submission_id}/share").status_code == 401


def test_only_one_active_share_per_submission(db, client, auth, shared):
    """부분 유니크 인덱스가 없으면 동시 요청 두 건이 각각 토큰을 만들고, 그러면
    폐기 API가 하나만 지워 **폐기했다고 응답한 링크가 계속 열린다.**"""
    db.add(SubmissionShare(submission_id=uuid.UUID(shared["submission_id"])))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


# ------------------------------------------------------------------ 공개 조회

def test_anyone_can_read_a_shared_analysis_without_logging_in(client, shared):
    """이 기능의 존재 이유. 인증 헤더 없이 열려야 한다."""
    res = client.get(f"/api/shared/{shared['token']}")
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["title"] == "Graph Neural Networks for Molecules"
    assert data["report"]["query_title"] == _REPORT["query_title"]


def test_public_response_exposes_nothing_beyond_the_agreed_fields(client, shared):
    """⚠️ 이 테스트가 실패했다면 스키마에 필드가 늘어난 것이다. 통과시키기 전에
    그 필드가 인터넷 전체에 공개돼도 되는지 먼저 판단할 것."""
    data = client.get(f"/api/shared/{shared['token']}").json()["data"]
    assert set(data) == PUBLIC_FIELDS

    # 개인정보와 원문은 어느 깊이에도 없어야 한다 (report 안까지 훑는다).
    body = client.get(f"/api/shared/{shared['token']}").text
    for leaked in ("user_id", "pdf_bytes", "@example.com", "tester",
                   shared["submission_id"]):
        assert leaked not in body, f"공개 응답에 {leaked!r}이 새어 나갔습니다"


def test_unknown_token_is_404(client):
    assert client.get("/api/shared/nope-not-a-real-token").status_code == 404


def test_revoked_token_stops_working(client, auth, shared):
    """폐기가 실제로 닫는지 — 공유를 되돌리는 유일한 수단이다."""
    res = client.delete(f"/api/submissions/{shared['submission_id']}/share",
                        headers=auth["headers"])
    assert res.status_code == 204, res.text
    assert client.get(f"/api/shared/{shared['token']}").status_code == 404


def test_revoked_and_missing_tokens_are_indistinguishable(client, auth, shared):
    """폐기된 토큰을 '폐기됨'으로 답하면 그 초안이 존재한다는 사실이 새어 나가고,
    대입 시도자에게는 '이 토큰은 맞았다'는 힌트가 된다."""
    client.delete(f"/api/submissions/{shared['submission_id']}/share",
                  headers=auth["headers"])
    revoked = client.get(f"/api/shared/{shared['token']}")
    unknown = client.get("/api/shared/some-token-that-never-existed-abcdefghij")
    assert revoked.status_code == unknown.status_code == 404
    assert revoked.json()["error"] == unknown.json()["error"]


# ------------------------------------------------------------------ 폐기

def test_revoking_is_idempotent(client, auth, shared):
    """두 번 눌렀다고 404를 주면 화면은 '실패'로 보이는데 실제 상태는 원하던 대로다."""
    path = f"/api/submissions/{shared['submission_id']}/share"
    assert client.delete(path, headers=auth["headers"]).status_code == 204
    assert client.delete(path, headers=auth["headers"]).status_code == 204


def test_stranger_cannot_revoke(client, other_user, shared):
    res = client.delete(f"/api/submissions/{shared['submission_id']}/share",
                        headers=other_user["headers"])
    assert res.status_code == 404, res.text
    assert client.get(f"/api/shared/{shared['token']}").status_code == 200


def test_resharing_issues_a_new_token_and_the_old_one_stays_dead(client, auth, shared):
    """폐기 뒤 재발급은 **새 토큰**이어야 한다. 같은 값이 다시 나오면 이미 회수한
    링크가 되살아난다."""
    path = f"/api/submissions/{shared['submission_id']}/share"
    client.delete(path, headers=auth["headers"])
    new_token = client.post(path, headers=auth["headers"]).json()["data"]["token"]

    assert new_token != shared["token"]
    assert client.get(f"/api/shared/{shared['token']}").status_code == 404
    assert client.get(f"/api/shared/{new_token}").status_code == 200


# ------------------------------------------------------- 다른 기능과의 관계

def test_reanalysis_does_not_break_a_shared_link(client, auth, db, shared):
    """소유자가 같은 초안을 다시 분석하는 동안에도 링크는 마지막 결과를 계속 보여준다.

    가장 최근 행만 보면 재분석 중에는 pending이라, 링크를 받은 사람 쪽에서는 잘
    보이던 결과가 갑자기 빈 화면이 된다 — 남의 재분석 때문에 내 링크가 깨진다.
    """
    db.add(ReviewPrediction(submission_id=uuid.UUID(shared["submission_id"]),
                            status="running"))
    db.commit()

    res = client.get(f"/api/shared/{shared['token']}")
    assert res.status_code == 200, res.text
    assert res.json()["data"]["report"]["query_title"] == _REPORT["query_title"]


def test_deleting_the_submission_kills_the_share(client, auth, db, shared):
    """초안을 지우면 링크도 죽어야 한다 (FK ON DELETE CASCADE). 남아 있으면
    없는 초안을 가리키는 공개 토큰이 떠돈다."""
    res = client.delete(f"/api/submissions/{shared['submission_id']}",
                        headers=auth["headers"])
    assert res.status_code == 204, res.text

    assert client.get(f"/api/shared/{shared['token']}").status_code == 404
    assert db.get(Submission, uuid.UUID(shared["submission_id"])) is None
    assert db.query(SubmissionShare).count() == 0
