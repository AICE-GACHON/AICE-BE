"""내 논문 업로드/조회와 소유권 경계.

입력은 **PDF 전용**이다. 텍스트 붙여넣기 경로(POST /api/submissions)는 없앴다 —
2단계 LLM 재정렬이 본문·참고문헌을 봐야 하는데 제목·초록만으로는 줄 수 없고,
텍스트 입력을 허용하면 같은 API가 전혀 다른 품질의 결과를 조용히 내게 된다.
"""
import uuid

from tests.app.conftest import make_pdf, upload_pdf

TITLE = "Graph Neural Networks for Molecules"
ABSTRACT = "We propose a message-passing GNN for molecules."


def _create(client, auth, **overrides):
    res = upload_pdf(client, auth, **overrides)
    assert res.status_code == 201, res.text
    return res.json()["data"]


# ------------------------------------------------------------------ 입력 경로

def test_json_upload_path_is_gone(client, auth):
    """텍스트만으로 논문을 올릴 수 있으면 안 된다.

    GET /api/submissions(목록)는 남아 있으므로 경로는 존재하고 메서드만 없다 → 405.
    """
    res = client.post("/api/submissions",
                      json={"title": TITLE, "abstract": ABSTRACT},
                      headers=auth["headers"])
    assert res.status_code == 405


def test_create_requires_auth(client, auth):
    res = client.post("/api/submissions/pdf",
                      files={"pdf": ("paper.pdf", make_pdf(), "application/pdf")})
    assert res.status_code == 401


def test_create_returns_owned_submission(client, auth):
    data = _create(client, auth)
    assert data["title"] == TITLE
    assert data["field"] == "ML"
    assert data["created_at"] is not None


def test_non_pdf_upload_is_rejected(client, auth):
    res = upload_pdf(client, auth, pdf=b"not a pdf at all", filename="notes.txt")
    assert res.status_code == 422


def test_corrupt_pdf_is_rejected(client, auth):
    """확장자만 pdf인 깨진 파일. 열리지 않으면 추출도 불가능하므로 즉시 거부한다."""
    res = upload_pdf(client, auth, pdf=b"%PDF-1.4 broken", title=None, abstract=None)
    assert res.status_code == 422


# --------------------------------------------------------------- 제목/초록 추출

def test_title_and_abstract_are_extracted_when_omitted(client, auth):
    """폼에서 빼면 서버가 PDF에서 뽑는다 — 이게 기본 경로다."""
    data = _create(client, auth, title=None, abstract=None)
    assert data["title"] == TITLE
    assert data["abstract"].startswith("We propose a message-passing GNN")


def test_user_supplied_title_wins_over_extraction(client, auth):
    """추출이 어긋났을 때 사용자가 고칠 수 있어야 한다."""
    data = _create(client, auth, title="사용자가 고친 제목")
    assert data["title"] == "사용자가 고친 제목"


def test_pdf_without_extractable_text_is_rejected(client, auth):
    """제목·초록이 안 나오면 1단계 임베딩 자체가 불가능하므로 422로 끊는다.

    스캔본을 흉내내려고 텍스트 레이어가 없는(그림만 있는) PDF를 만든다.
    """
    import fitz

    doc = fitz.open()
    doc.new_page()          # 빈 페이지 — 텍스트 span이 하나도 없다
    blank = doc.tobytes()
    doc.close()

    res = upload_pdf(client, auth, pdf=blank, title=None, abstract=None)
    assert res.status_code == 422


# ------------------------------------------------------------------ 페이지 수

def test_page_count_is_returned(client, auth):
    """프론트가 '15p 초과' 경고를 띄우려면 이 값이 필요하다."""
    data = _create(client, auth, pages=3)
    assert data["page_count"] == 3


def test_long_document_is_rejected(client, auth):
    """60페이지 초과는 논문이 아닐 개연성이 높고, PDF 블록 비용이 페이지에 비례한다."""
    res = upload_pdf(client, auth, pages=61)
    assert res.status_code == 422
    assert "61" in res.json()["error"]["message"]


def test_page_count_at_the_limit_is_accepted(client, auth):
    """경계값은 통과해야 한다 — 상한은 '초과'부터다."""
    res = upload_pdf(client, auth, pages=60)
    assert res.status_code == 201


def test_warning_threshold_does_not_block_upload(client, auth):
    """15p는 경고선이지 거부선이 아니다. 경고는 프론트가 page_count를 보고 띄운다."""
    data = _create(client, auth, pages=20)
    assert data["page_count"] == 20


# ------------------------------------------------------------------ PDF 저장

def test_pdf_bytes_are_stored_for_the_background_analysis(client, auth, db):
    """분석은 응답 이후에 도는 백그라운드 작업이라, 업로드 때 저장해 두지 않으면
    그때 가서 PDF를 다시 구할 방법이 없다."""
    from app.models.submission import Submission

    data = _create(client, auth)
    row = db.get(Submission, uuid.UUID(data["submission_id"]))
    assert row.pdf_bytes is not None
    assert row.pdf_bytes.startswith(b"%PDF")


def test_pdf_bytes_never_leak_into_responses(client, auth):
    """20MB 블롭이 응답에 실리면 매 조회가 수십 MB가 된다."""
    data = _create(client, auth)
    assert "pdf_bytes" not in data

    detail = client.get(f"/api/submissions/{data['submission_id']}",
                        headers=auth["headers"]).json()["data"]
    assert "pdf_bytes" not in detail


# ------------------------------------------------------------ 목록·상세·삭제

def test_list_returns_only_my_submissions(client, auth, other_user):
    _create(client, auth, title="내 논문")
    _create(client, other_user, title="남의 논문")

    titles = [s["title"] for s in
              client.get("/api/submissions", headers=auth["headers"]).json()["data"]]
    assert titles == ["내 논문"]


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
    assert res.json()["data"]["abstract"] == ABSTRACT


def test_get_others_submission_is_404_not_403(client, auth, other_user):
    """남의 논문은 존재 여부조차 알려주지 않는다."""
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
