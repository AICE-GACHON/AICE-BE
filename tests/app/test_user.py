"""내 정보 수정 / 회원 탈퇴 (PATCH·DELETE /api/user/me).

여기서 지키려는 것은 두 가지다:
  - **되돌릴 수 없는 일에는 비밀번호를 다시 묻는다.** access_token 하나로 비밀번호를
    바꾸거나 계정을 지울 수 있으면, 토큰이 유출됐을 때 원래 주인이 계정을 잃는다.
  - **중복 openreview_id는 409지 500이 아니다.** 컬럼이 unique라 그냥 commit하면
    IntegrityError가 그대로 500으로 새어 나간다.
"""
import uuid

import pytest

from tests.app.conftest import _unique_openreview_id


def _patch(client, auth, **body):
    return client.patch("/api/user/me", json=body, headers=auth["headers"])


# ------------------------------------------------------------ 프로필 수정

def test_updates_only_the_fields_sent(client, auth):
    """보낸 필드만 바뀌고 나머지는 그대로 남는다."""
    before = client.get("/api/user/me", headers=auth["headers"]).json()["data"]

    res = _patch(client, auth, nickname="새이름")
    assert res.status_code == 200, res.text
    after = res.json()["data"]
    assert after["nickname"] == "새이름"
    assert after["openreview_id"] == before["openreview_id"]
    assert after["email"] == before["email"]


def test_empty_body_is_a_no_op(client, auth):
    """전부 선택 필드라 빈 body도 유효하다 — 아무것도 안 바뀌면 된다."""
    before = client.get("/api/user/me", headers=auth["headers"]).json()["data"]
    res = _patch(client, auth)
    assert res.status_code == 200, res.text
    assert res.json()["data"]["nickname"] == before["nickname"]


def test_duplicate_openreview_id_is_409_not_500(client, auth, other_user):
    """남이 쓰는 openreview_id로 바꾸려 하면 409.

    사전 조회 없이 unique 제약에 맡기고 IntegrityError를 409로 바꾼다. 이 처리가
    없으면 500이 난다 — 실제로 그랬다.
    """
    taken = client.get("/api/user/me", headers=other_user["headers"]).json()["data"]

    res = _patch(client, auth, openreview_id=taken["openreview_id"])
    assert res.status_code == 409, res.text

    # 롤백이 제대로 됐는지 — 실패한 요청이 세션을 망가뜨리면 다음 요청이 깨진다.
    assert client.get("/api/user/me", headers=auth["headers"]).status_code == 200


def test_rejects_unauthenticated(client):
    assert client.patch("/api/user/me", json={"nickname": "x"}).status_code == 401
    assert client.delete("/api/user/me").status_code == 401


# ---------------------------------------------------------- 비밀번호 변경

def test_password_change_switches_the_working_password(client, auth):
    res = _patch(client, auth,
                 current_password=auth["password"], new_password="newpassword456")
    assert res.status_code == 200, res.text

    old = client.post("/api/auth/login",
                      json={"email": auth["email"], "password": auth["password"]})
    assert old.status_code == 401, "옛 비밀번호가 아직 통한다"

    new = client.post("/api/auth/login",
                      json={"email": auth["email"], "password": "newpassword456"})
    assert new.status_code == 200, new.text


def test_password_change_requires_the_current_one(client, auth):
    res = _patch(client, auth,
                 current_password="wrong-password", new_password="newpassword456")
    assert res.status_code == 401, res.text

    # 실패했으면 비밀번호가 그대로여야 한다.
    assert client.post("/api/auth/login", json={
        "email": auth["email"], "password": auth["password"]}).status_code == 200


def test_new_password_without_current_is_rejected_before_the_db(client, auth):
    """스키마 단계에서 막는다 — 라우터까지 가면 검증을 빠뜨리기 쉽다."""
    res = client.patch("/api/user/me", json={"new_password": "newpassword456"},
                       headers=auth["headers"])
    assert res.status_code == 422, res.text


def test_password_change_revokes_existing_refresh_tokens(client, auth):
    """비밀번호를 바꾸면 이미 발급된 refresh_token은 죽어야 한다.

    안 그러면 유출된 비밀번호로 받아간 refresh_token이 계속 살아 있어서, 비밀번호를
    바꾼 의미가 없다. token_version을 올리는 것으로 처리한다(로그아웃과 같은 방식).
    """
    tokens = client.post("/api/auth/login", json={
        "email": auth["email"], "password": auth["password"]}).json()["data"]

    assert _patch(client, auth, current_password=auth["password"],
                  new_password="newpassword456").status_code == 200

    res = client.post("/api/auth/refresh",
                      json={"refresh_token": tokens["refresh_token"]})
    assert res.status_code == 401, "옛 refresh_token이 아직 살아 있다"


def test_password_and_profile_change_together(client, auth):
    res = _patch(client, auth, nickname="한번에",
                 current_password=auth["password"], new_password="newpassword456")
    assert res.status_code == 200, res.text
    assert res.json()["data"]["nickname"] == "한번에"
    assert client.post("/api/auth/login", json={
        "email": auth["email"], "password": "newpassword456"}).status_code == 200


@pytest.mark.parametrize("bad", ["short", "가" * 25])
def test_new_password_follows_signup_rules(client, auth, bad):
    """8자 미만, 그리고 bcrypt 72바이트 초과(한글 25자=75바이트)를 막는다.

    가입 때만 막고 변경 때 안 막으면, 변경 경로로 규칙을 우회할 수 있다.
    """
    res = _patch(client, auth, current_password=auth["password"], new_password=bad)
    assert res.status_code == 422, res.text


# --------------------------------------------------------------- 회원 탈퇴

def test_delete_requires_the_password(client, auth):
    """비밀번호 없이 오는 탈퇴 요청은 400. 되돌릴 수 없는 일이라 확인을 요구한다."""
    res = client.delete("/api/user/me", headers=auth["headers"])
    assert res.status_code == 400, res.text
    assert client.get("/api/user/me", headers=auth["headers"]).status_code == 200


def test_delete_rejects_a_wrong_password(client, auth):
    res = client.request("DELETE", "/api/user/me", json={"password": "wrong-password"},
                         headers=auth["headers"])
    assert res.status_code == 401, res.text
    assert client.get("/api/user/me", headers=auth["headers"]).status_code == 200


def test_delete_removes_the_account(client, auth):
    res = client.request("DELETE", "/api/user/me", json={"password": auth["password"]},
                         headers=auth["headers"])
    assert res.status_code == 200, res.text

    # 계정이 사라졌으니 같은 토큰으로 더는 조회되지 않는다.
    assert client.get("/api/user/me", headers=auth["headers"]).status_code == 401
    assert client.post("/api/auth/login", json={
        "email": auth["email"], "password": auth["password"]}).status_code == 401


# ------------------------------------------------- 구글 전용 계정 (비밀번호 없음)

@pytest.fixture
def google_only(client, monkeypatch):
    """구글로만 가입한 계정 — password_hash가 없다."""
    unique = uuid.uuid4().hex[:12]
    claims = {"sub": f"google-sub-{unique}", "email": f"g_{unique}@example.com",
              "email_verified": True, "name": "구글사용자"}
    monkeypatch.setattr("app.routers.auth.verify_google_id_token",
                        lambda token: claims)
    res = client.post("/api/auth/google", json={
        "id_token": "fake", "openreview_id": _unique_openreview_id(),
        "agreed_to_terms": True})
    assert res.status_code == 200, res.text
    token = res.json()["data"]["access_token"]
    return {"headers": {"Authorization": f"Bearer {token}"}}


def test_google_only_account_cannot_set_a_password(client, google_only):
    """대조할 기존 비밀번호가 없다 — 400으로 분명히 알려준다."""
    res = client.patch("/api/user/me",
                       json={"current_password": "anything",
                             "new_password": "newpassword456"},
                       headers=google_only["headers"])
    assert res.status_code == 400, res.text


def test_google_only_account_can_still_update_profile(client, google_only):
    res = client.patch("/api/user/me", json={"nickname": "구글닉"},
                       headers=google_only["headers"])
    assert res.status_code == 200, res.text
    assert res.json()["data"]["nickname"] == "구글닉"


def test_google_only_account_deletes_without_a_password(client, google_only):
    """비밀번호를 요구하면 구글 전용 계정은 영영 탈퇴할 수 없다."""
    res = client.delete("/api/user/me", headers=google_only["headers"])
    assert res.status_code == 200, res.text
    assert client.get("/api/user/me", headers=google_only["headers"]).status_code == 401


# ------------------------------------------------------------- has_password
#
# 프론트가 "비밀번호 변경 UI를 띄울지", "탈퇴에 비밀번호를 물을지"를 판단하는
# 유일한 근거다. google_linked로는 판단할 수 없다는 것이 아래 세 번째 테스트다.

def test_password_account_reports_has_password(client, auth):
    body = client.get("/api/user/me", headers=auth["headers"]).json()["data"]
    assert body["has_password"] is True
    assert "password_hash" not in body  # 있고 없고만 알려준다


def test_google_only_account_reports_no_password(client, google_only):
    body = client.get("/api/user/me", headers=google_only["headers"]).json()["data"]
    assert body["has_password"] is False
    assert body["google_linked"] is True


def test_google_linked_email_account_still_has_a_password(client, auth, monkeypatch):
    """이메일로 가입한 계정에 구글을 연동해도 비밀번호는 남아 있다.

    **이 경우가 has_password를 만든 이유다.** 여기서 google_linked와 has_password가
    둘 다 True로 갈린다 — google_linked만 보고 "구글이니까 비밀번호 없음"으로
    분기하면, 비밀번호를 바꿀 수 있는 사람에게 변경 UI가 사라지고 탈퇴 확인에도
    입력칸이 안 뜬다(그런데 서버는 비밀번호를 요구하므로 400이다).
    """
    claims = {"sub": f"google-sub-{uuid.uuid4().hex[:12]}", "email": auth["email"],
              "email_verified": True, "name": "연동"}
    monkeypatch.setattr("app.routers.auth.verify_google_id_token", lambda token: claims)
    assert client.post("/api/auth/google", json={"id_token": "fake"}).status_code == 200

    body = client.get("/api/user/me", headers=auth["headers"]).json()["data"]
    assert body["google_linked"] is True
    assert body["has_password"] is True

    # 실제로도 비밀번호가 살아 있어야 한다 — 플래그만 맞고 동작이 다르면 소용없다
    assert client.post("/api/auth/login", json={
        "email": auth["email"], "password": auth["password"]}).status_code == 200


# --------------------------------------------- 온보딩 답변 수정 (PATCH /me/onboarding)

def _patch_onboarding(client, auth, **body):
    return client.patch("/api/user/me/onboarding", json=body, headers=auth["headers"])


def test_onboarding_patch_creates_when_missing(client, auth):
    """행이 없으면 만든다 (upsert).

    온보딩을 건너뛰고 가입한 계정은 행이 아예 없다. 404를 돌려주면 "먼저
    만드세요"가 되는데, 온보딩은 가입 전에만 지나가는 흐름이라 다시 갈 수 없다.
    """
    assert client.get("/api/user/me/onboarding", headers=auth["headers"]).status_code == 404

    res = _patch_onboarding(client, auth, user_type="researcher", venue=["ICLR"])
    assert res.status_code == 200, res.text
    assert res.json()["data"]["user_type"] == "researcher"

    after = client.get("/api/user/me/onboarding", headers=auth["headers"])
    assert after.status_code == 200
    assert after.json()["data"]["venue"] == ["ICLR"]


def test_onboarding_patch_updates_only_the_fields_sent(client, auth):
    """**안 보낸 필드는 그대로 남는다.**

    exclude_unset이 빠지면 여기서 걸린다 — venue 하나 고치려던 요청이
    fields를 기본값 []로 덮어써 답변이 통째로 사라진다.
    """
    _patch_onboarding(client, auth, user_type="student",
                      fields=["ML", "NLP"], venue=["ICLR"])

    res = _patch_onboarding(client, auth, venue=["NeurIPS"])
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["venue"] == ["NeurIPS"]
    assert data["fields"] == ["ML", "NLP"]
    assert data["user_type"] == "student"


def test_onboarding_patch_can_clear_a_list_with_an_empty_array(client, auth):
    """[]는 "안 보냄"과 달리 의도적으로 비운 것이다."""
    _patch_onboarding(client, auth, fields=["ML"])
    res = _patch_onboarding(client, auth, fields=[])
    assert res.status_code == 200, res.text
    assert res.json()["data"]["fields"] == []


def test_onboarding_patch_null_list_is_not_a_500(client, auth):
    """리스트 컬럼은 nullable=False다. 명시적 null이 DB까지 가면 500이 난다.

    "안 보낸 것"과 같이 취급해 무시한다 — 비우려면 []를 보내면 된다.
    """
    _patch_onboarding(client, auth, fields=["ML"])
    res = _patch_onboarding(client, auth, fields=None)
    assert res.status_code == 200, res.text
    assert res.json()["data"]["fields"] == ["ML"]


def test_onboarding_patch_null_venue_is_not_a_500(client, auth):
    """venue도 fields와 같은 JSONB 리스트라 같은 함정이 있다."""
    _patch_onboarding(client, auth, venue=["ICLR"])
    res = _patch_onboarding(client, auth, venue=None)
    assert res.status_code == 200, res.text
    assert res.json()["data"]["venue"] == ["ICLR"]


def test_onboarding_patch_keeps_answers_separate_between_users(client, auth, other_user):
    """남의 답변을 고치거나 읽으면 안 된다."""
    _patch_onboarding(client, auth, venue=["ICLR"])
    _patch_onboarding(client, other_user, venue=["NeurIPS"])

    mine = client.get("/api/user/me/onboarding", headers=auth["headers"]).json()["data"]
    theirs = client.get("/api/user/me/onboarding", headers=other_user["headers"]).json()["data"]
    assert mine["venue"] == ["ICLR"]
    assert theirs["venue"] == ["NeurIPS"]
    assert mine["onboarding_id"] != theirs["onboarding_id"]


def test_onboarding_patch_rejects_unauthenticated(client):
    assert client.patch("/api/user/me/onboarding", json={"venue": ["ICLR"]}).status_code == 401


def test_onboarding_patch_enforces_the_same_limits_as_create(client, auth):
    """수정이 생성보다 느슨하면 그쪽이 우회로가 된다 (venue 원소는 Item — 100자)."""
    assert _patch_onboarding(client, auth, venue=["X" * 101]).status_code == 422
