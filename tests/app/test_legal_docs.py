"""약관·개인정보처리방침 원문 조회 (GET /api/legal/{document}).

여기서 지키는 것은 셋이다:

  1. **로그인 없이 읽힌다.** 동의를 받아야 하는 화면이 계정 생성 *전*이라,
     인증을 걸면 바로 그 화면에서 못 읽는다.
  2. **버전이 어긋나지 않는다.** 화면에 보여준 문서의 버전과 가입 시 users에
     찍히는 버전이 다르면, 동의 이력이 "무엇에 대한 동의"인지 말해주지 못한다.
  3. **배포 이미지에 문서가 실린다.** .dockerignore가 `docs/`와 `**/*.md`를
     제외하므로, 예외 한 줄이 사라지면 로컬은 멀쩡하고 배포만 빈 화면이 된다.
"""
import re

import pytest

from app.core.legal import PRIVACY_VERSION, TERMS_VERSION, load_document

DOCS = {"terms": TERMS_VERSION, "privacy": PRIVACY_VERSION}


@pytest.mark.parametrize("document", sorted(DOCS))
def test_document_is_readable_without_a_token(client, document):
    """🔴 인증을 걸면 회원가입 화면에서 약관을 못 읽는다 — 동의를 받아야 하는
    바로 그 화면이다."""
    res = client.get(f"/api/legal/{document}")
    assert res.status_code == 200, res.text

    data = res.json()["data"]
    assert data["document"] == document
    assert data["format"] == "markdown"
    assert data["title"].startswith("AICE")
    assert len(data["content"]) > 500  # 빈 파일이나 자리표시자만 남은 상태 방지


@pytest.mark.parametrize("document,expected", sorted(DOCS.items()))
def test_served_version_is_the_version_stamped_on_consent(client, document, expected):
    """응답의 버전은 가입 시 users에 찍히는 상수와 같아야 한다.

    둘이 갈라지면 "1.1을 보여주고 1.0에 동의시켰다"가 조용히 성립한다.
    """
    res = client.get(f"/api/legal/{document}")
    assert res.json()["data"]["version"] == expected


@pytest.mark.parametrize("document,expected", sorted(DOCS.items()))
def test_document_header_matches_the_constant(document, expected):
    """문서 머리말의 `- 버전:` 줄과 app/core/legal.py 상수가 같은지.

    사람이 읽는 것은 문서 머리말이고 코드가 찍는 것은 상수다. 문서만 고치고
    상수를 안 올리면(또는 그 반대면) 아무도 오류를 못 보는 채로 어긋난다.
    """
    content = load_document(document).content
    header = re.search(r"^- 버전:\s*(\S+)", content, re.MULTILINE)
    assert header is not None, f"{document} 문서에 '- 버전:' 줄이 없다"
    assert header.group(1) == expected


def test_unknown_document_is_404_not_a_file_read(client):
    """허용 목록에 없는 이름은 파일을 열어보기도 전에 404다.

    같은 디렉터리에 README.md가 있으므로, 이름만 맞히면 나가는 구조였다면
    개발용 메모가 그대로 공개된다.
    """
    assert client.get("/api/legal/README").status_code == 404
    assert client.get("/api/legal/../core/config").status_code in (404, 405)


def test_documents_are_not_excluded_from_the_docker_image():
    """🔴 .dockerignore의 예외가 살아 있는지.

    `docs/`와 `**/*.md`가 제외 대상이라, `!app/legal/*.md` 한 줄이 사라지면
    **로컬 테스트는 전부 통과하면서 배포 이미지에서만 약관이 사라진다.** 그 사고는
    빌드도 기동도 막지 않기 때문에, 사용자가 빈 약관 화면을 보고 알려줄 때까지
    아무도 모른다. 그래서 여기서 못박는다.
    """
    from pathlib import Path

    ignore = Path(__file__).resolve().parents[2] / ".dockerignore"
    patterns = [line.strip() for line in ignore.read_text(encoding="utf-8").splitlines()]
    assert "!app/legal/*.md" in patterns, (
        ".dockerignore에서 app/legal/*.md 예외가 사라졌다. 이대로 배포하면 "
        "약관·개인정보처리방침이 이미지에 안 실려 회원가입 화면이 빈다.")


@pytest.mark.parametrize("document", sorted(DOCS))
def test_no_unfilled_placeholders_are_served(client, document):
    """자리표시자가 그대로 사용자에게 나가지 않는지.

    한 번 다 채운 뒤에는 이게 회귀 방지선이 된다 — 문서를 개정하면서 새 항목을
    `{{ }}`로 적어두고 잊으면, 회원가입 화면에 "{{운영주체명}}(이하 "회사")"가
    그대로 찍힌다. 서버는 경고만 남기고 계속 뜨므로 로그를 안 보면 모른다.
    """
    content = client.get(f"/api/legal/{document}").json()["data"]["content"]
    assert "{{" not in content, f"{document}에 채우지 않은 자리표시자가 있다"


def test_developer_notes_are_not_served(client):
    """개발자용 경고문("초안입니다", "법률 검토를 받으세요")이 사용자에게 나가면 안 된다.

    그 문구는 app/legal/README.md에 있고, README는 서빙 대상이 아니다.
    """
    for document in DOCS:
        content = client.get(f"/api/legal/{document}").json()["data"]["content"]
        assert "초안" not in content
        assert "법률 검토" not in content
