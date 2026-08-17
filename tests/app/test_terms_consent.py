"""약관·개인정보처리방침 동의 게이트와 동의 이력 기록.

약관을 게시하는 이유는 "동의를 받았다"가 아니라 **"이 사람이 언제 어느 버전에
동의했는지 증명할 수 있다"** 이다. 그래서 이 파일은 두 가지를 지킨다:

  1. 동의 없이 계정이 만들어지지 않는다 — **가입 경로 둘 다에서.**
     초대 게이트와 똑같은 함정이 있다(test_invite_gate.py 참고): /signup만 막으면
     구글 버튼으로 동의 없는 계정이 그대로 생긴다.
  2. 동의 시각과 **버전**이 함께 남는다. 시각만 남기면 문서를 한 번 개정한
     순간부터 아무것도 증명하지 못한다.
"""
import uuid

import pytest

from app.core.legal import PRIVACY_VERSION, TERMS_VERSION
from app.models.user import User
from tests.app.conftest import _unique_email, _unique_openreview_id


def _signup_payload(**overrides) -> dict:
    payload = {
        "email": _unique_email(),
        "password": "password123",
        "nickname": "tester",
        "openreview_id": _unique_openreview_id(),
    }
    payload.update(overrides)
    return payload


def _claims(**overrides) -> dict:
    unique = uuid.uuid4().hex[:12]
    return {"sub": f"google-sub-{unique}", "email": f"g_{unique}@example.com",
            "email_verified": True, "name": "구글사용자", **overrides}


@pytest.fixture
def google_claims(monkeypatch):
    """라우터가 보게 될 구글 클레임을 테스트가 정해준다.

    라우터가 `from ... import verify_google_id_token`으로 이름을 당겨왔으므로
    원본 모듈이 아니라 라우터 네임스페이스를 갈아끼워야 한다.
    """
    box = {}
    monkeypatch.setattr("app.routers.auth.verify_google_id_token",
                        lambda token: box["claims"])

    def _set(claims: dict) -> dict:
        box["claims"] = claims
        return claims

    return _set


# --------------------------------------------------------------- 이메일 가입

def test_signup_rejected_without_consent(client):
    res = client.post("/api/auth/signup", json=_signup_payload())
    assert res.status_code == 400, res.text


def test_signup_rejected_with_explicit_false(client):
    """필드를 안 보내는 것과 false로 보내는 것은 같아야 한다.

    기본값이 False라 두 경우가 라우터에서 구분되지 않는데, 그게 의도다 —
    "동의 안 함"이 두 가지 코드로 갈리면 프론트가 한쪽만 처리하게 된다.
    """
    res = client.post("/api/auth/signup", json=_signup_payload(agreed_to_terms=False))
    assert res.status_code == 400, res.text


def test_signup_records_time_and_version(client, db):
    """🔴 이 파일의 본론. 동의는 **시각과 버전이 함께** 남아야 한다.

    시각만 검사하는 테스트는 통과하면서도 아무것도 못 잡는다 — 문서를 개정한
    뒤에는 "2026-08-16에 동의함"이 어느 문구에 대한 동의인지 말해주지 않는다.
    """
    email = _unique_email()
    res = client.post("/api/auth/signup",
                      json=_signup_payload(email=email, agreed_to_terms=True))
    assert res.status_code == 201, res.text

    user = db.query(User).filter(User.email == email).one()
    assert user.terms_agreed_at is not None
    assert user.terms_version == TERMS_VERSION
    assert user.privacy_version == PRIVACY_VERSION


# ----------------------------------------------------------------- 구글 가입

def test_google_signup_is_gated_too(client, google_claims):
    """🔴 이 테스트가 이 파일의 존재 이유다 (초대 게이트와 같은 이유).

    /signup만 막고 이쪽을 빠뜨리면, 동의를 받고 있다고 믿는 상태로 구글 버튼을
    누른 사람 전원이 동의 이력 없는 계정이 된다. 그리고 그 사실은 분쟁이 생겨
    이력을 꺼내볼 때까지 아무도 모른다.
    """
    google_claims(_claims())
    res = client.post("/api/auth/google", json={"id_token": "fake-id-token"})
    assert res.status_code == 400, res.text


def test_google_signup_records_consent(client, db, google_claims):
    claims = google_claims(_claims())
    res = client.post("/api/auth/google", json={
        "id_token": "fake-id-token", "agreed_to_terms": True})
    assert res.status_code == 200, res.text

    user = db.query(User).filter(User.email == claims["email"]).one()
    assert user.terms_agreed_at is not None
    assert user.terms_version == TERMS_VERSION


def test_existing_google_account_logs_in_without_consenting_again(client, google_claims):
    """이미 있는 계정의 로그인에는 동의를 다시 요구하지 않는다.

    매번 물으면 그건 동의가 아니라 확인 버튼이다. 더 중요한 건 반대 방향인데 —
    재동의가 필요하다고 로그인을 막아버리면 그 사용자는 재동의도, 탈퇴도, 자기
    자료를 내려받는 것도 할 수 없게 된다.
    """
    google_claims(_claims())
    first = client.post("/api/auth/google", json={
        "id_token": "fake-id-token", "agreed_to_terms": True})
    assert first.status_code == 200, first.text

    # 같은 구글 계정으로 다시 로그인 — 이번엔 동의 필드가 없다
    again = client.post("/api/auth/google", json={"id_token": "fake-id-token"})
    assert again.status_code == 200, again.text


# ------------------------------------------------------- 재동의 판정(개정 대응)

def test_consent_up_to_date_is_true_right_after_signup(client, auth):
    body = client.get("/api/user/me", headers=auth["headers"]).json()["data"]
    assert body["consent_up_to_date"] is True


def test_document_revision_makes_existing_consent_stale(client, auth, monkeypatch):
    """문서를 개정하면(legal.py 버전 상승) 기존 사용자가 재동의 대상이 된다.

    ⚠️ **monkeypatch 대상은 app.core.legal이 아니라 app.models.user다.**
    User가 `from app.core.legal import TERMS_VERSION`으로 값을 당겨왔기 때문에,
    원본 모듈만 바꾸면 이미 바인딩된 이름은 그대로다 — 테스트가 조용히 통과하고
    (실제로는 아무것도 검증하지 않고) 개정 대응이 깨진 채 배포된다.
    """
    monkeypatch.setattr("app.models.user.TERMS_VERSION", "99.0")

    body = client.get("/api/user/me", headers=auth["headers"]).json()["data"]
    assert body["consent_up_to_date"] is False


def test_reconsent_endpoint_clears_the_stale_flag(client, db, auth, monkeypatch):
    """🔴 재동의 경로가 없으면 사용자가 갇힌다.

    개정 뒤에는 화면이 "재동의가 필요합니다"를 계속 띄우는데, 그 상태에서
    빠져나갈 API가 없으면 사용자는 배너를 영원히 보게 된다.
    """
    monkeypatch.setattr("app.models.user.TERMS_VERSION", "99.0")
    assert client.get("/api/user/me", headers=auth["headers"]).json()["data"][
        "consent_up_to_date"] is False

    res = client.post("/api/user/me/consent", headers=auth["headers"])
    assert res.status_code == 200, res.text
    assert res.json()["data"]["consent_up_to_date"] is True

    # 응답만 맞고 저장이 안 되면 다음 새로고침에 배너가 되살아난다 — DB를 본다.
    user = db.query(User).filter(User.email == auth["email"]).one()
    assert user.terms_version == "99.0"


def test_reconsent_requires_login(client):
    """로그인 없이 남의 동의를 대신 찍을 수 없어야 한다."""
    assert client.post("/api/user/me/consent").status_code == 401


def test_account_without_consent_history_is_stale(client, db, auth):
    """동의 컬럼이 생기기 전에 가입한 계정(=전부 null)도 재동의 대상이다.

    마이그레이션에서 기본값을 채우지 않은 것이 여기서 의미를 갖는다 — 채웠다면
    받은 적 없는 동의가 최신으로 보이고, 이 사람들에게는 재동의 화면이 영영
    뜨지 않는다 (alembic 0015).
    """
    user = db.query(User).filter(User.email == auth["email"]).one()
    user.terms_agreed_at = None
    user.terms_version = None
    user.privacy_version = None
    db.commit()

    body = client.get("/api/user/me", headers=auth["headers"]).json()["data"]
    assert body["consent_up_to_date"] is False
