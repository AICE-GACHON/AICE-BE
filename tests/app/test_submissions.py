"""초안 CRUD와 소유권 경계."""
import uuid

DRAFT = {"title": "Graph Neural Networks for Molecules",
         "abstract": "We propose a message-passing GNN.",
         "field": "ML"}


def _create(client, auth, **overrides):
    payload = {**DRAFT, **overrides}
    res = client.post("/api/submissions", json=payload, headers=auth["headers"])
    assert res.status_code == 201, res.text
    return res.json()["data"]


def test_create_requires_auth(client):
    assert client.post("/api/submissions", json=DRAFT).status_code == 401


def test_create_returns_owned_submission(client, auth):
    data = _create(client, auth)
    assert data["title"] == DRAFT["title"]
    assert data["field"] == "ML"
    assert data["created_at"] is not None


def test_create_rejects_empty_title(client, auth):
    res = client.post("/api/submissions", json={**DRAFT, "title": ""},
                      headers=auth["headers"])
    assert res.status_code == 422


def test_list_returns_only_my_submissions(client, auth, other_user):
    _create(client, auth, title="내 초안")
    _create(client, other_user, title="남의 초안")

    titles = [s["title"] for s in
              client.get("/api/submissions", headers=auth["headers"]).json()["data"]]
    assert titles == ["내 초안"]


def test_list_is_newest_first(client, auth, db):
    from datetime import timedelta

    from app.models.submission import Submission

    older = _create(client, auth, title="첫 번째")
    newer = _create(client, auth, title="두 번째")
    # 두 행은 같은 테스트 트랜잭션에서 만들어져 created_at이 동률이다
    # (postgres의 now()는 트랜잭션 시각). 정렬을 검증하려면 시각을 벌려야 한다.
    row = db.get(Submission, uuid.UUID(older["submission_id"]))
    row.created_at = row.created_at - timedelta(minutes=1)
    db.commit()

    titles = [s["title"] for s in
              client.get("/api/submissions", headers=auth["headers"]).json()["data"]]
    assert titles == ["두 번째", "첫 번째"]
    assert newer["submission_id"] != older["submission_id"]


def test_get_returns_detail(client, auth):
    created = _create(client, auth)
    res = client.get(f"/api/submissions/{created['submission_id']}",
                     headers=auth["headers"])
    assert res.status_code == 200
    assert res.json()["data"]["abstract"] == DRAFT["abstract"]


def test_get_others_submission_is_404_not_403(client, auth, other_user):
    """남의 초안은 존재 여부조차 알려주지 않는다."""
    created = _create(client, other_user)
    res = client.get(f"/api/submissions/{created['submission_id']}",
                     headers=auth["headers"])
    assert res.status_code == 404


def test_get_missing_submission_is_404(client, auth):
    res = client.get(f"/api/submissions/{uuid.uuid4()}", headers=auth["headers"])
    assert res.status_code == 404


def test_delete_removes_submission(client, auth):
    created = _create(client, auth)
    sid = created["submission_id"]
    assert client.delete(f"/api/submissions/{sid}",
                         headers=auth["headers"]).status_code == 204
    assert client.get(f"/api/submissions/{sid}",
                      headers=auth["headers"]).status_code == 404


def test_cannot_delete_others_submission(client, auth, other_user):
    created = _create(client, other_user)
    res = client.delete(f"/api/submissions/{created['submission_id']}",
                        headers=auth["headers"])
    assert res.status_code == 404
    # 남의 것은 그대로 남아 있어야 한다
    assert client.get(f"/api/submissions/{created['submission_id']}",
                      headers=other_user["headers"]).status_code == 200
