"""게시 중인 약관·개인정보처리방침 — 버전과 원문 로딩.

문서 원본은 `app/legal/terms.md`, `app/legal/privacy.md`에 있고, 각 문서 머리말의
`버전:` 줄과 **여기 값이 같아야 한다** (tests/app/test_legal_docs.py가 검사한다).

**문서를 고치면 여기 버전도 함께 올려야 한다.** 이 값이 사용자가 동의할 때
users 테이블에 찍히므로(User.terms_version), 문서만 바꾸고 버전을 그대로 두면
"이 사람이 어느 문구에 동의했는가"를 나중에 증명할 수 없다 — 동의 이력을 남기는
이유 자체가 사라진다.

버전을 올리면 기존 사용자는 자동으로 `consent_up_to_date=False`가 되고, 프론트가
그걸 보고 재동의를 받는다. 그러니 오탈자 수정처럼 내용이 바뀌지 않는 편집에는
올리지 말 것 — 전체 사용자에게 재동의 화면이 뜬다.
"""
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

TERMS_VERSION = "1.0"
PRIVACY_VERSION = "1.0"

# --------------------------------------------------------------------- 원문

log = logging.getLogger(__name__)

# app/core/legal.py → app/legal/. **cwd가 아니라 이 파일 기준으로 잡는다** —
# uvicorn을 어디서 띄우든, 배치 스크립트가 어디서 부르든 같은 경로여야 한다.
_LEGAL_DIR = Path(__file__).resolve().parents[1] / "legal"

# 서빙 가능한 문서. 여기 없는 이름은 라우터가 404로 돌려준다 — README.md 같은
# 개발 문서가 같은 디렉터리에 있어도 밖으로 나가지 않는다.
DOCUMENTS: dict[str, tuple[str, str]] = {
    "terms": ("terms.md", TERMS_VERSION),
    "privacy": ("privacy.md", PRIVACY_VERSION),
}


@dataclass(frozen=True)
class LegalDocument:
    document: str
    title: str
    version: str
    content: str


def _title_of(text: str, fallback: str) -> str:
    """첫 번째 `# 제목` 줄. 화면 상단에 쓰라고 뽑아준다."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


@lru_cache(maxsize=None)
def load_document(document: str) -> LegalDocument:
    """문서 원문을 읽어 돌려준다. 프로세스 수명 동안 한 번만 읽는다.

    **version은 파일이 아니라 위 상수에서 온다.** 파일 머리말에서 파싱하면 문서를
    고치는 것만으로 동의 이력에 찍히는 버전이 바뀌는데, 그 값은 회원가입 시점에
    users에 저장되는 값이라 코드가 단일 소스여야 한다. 둘이 어긋나면 테스트가
    잡는다(tests/app/test_legal_docs.py).

    캐시하는 이유는 성능이 아니라 안정성이다 — 회원가입 화면이 부르는 공개
    엔드포인트라, 요청마다 디스크를 때리게 두면 그게 그대로 공격 표면이 된다.
    대신 로컬에서 문서를 고치면 재기동해야 반영된다.
    """
    filename, version = DOCUMENTS[document]
    text = (_LEGAL_DIR / filename).read_text(encoding="utf-8")

    # 채우지 않은 자리표시자가 그대로 사용자에게 보이는 사고를 막는다. 기동을
    # 막지는 않는다 — 약관 문구가 덜 채워진 것이 서버를 못 뜨게 할 일은 아니고,
    # 오히려 배포가 막히면 급할 때 자리표시자째 배포하는 우회가 생긴다.
    if "{{" in text:
        log.warning("%s에 채우지 않은 항목({{...}})이 남아 있습니다 — 사용자에게 그대로 "
                    "보입니다. app/legal/README.md 참고.", filename)

    return LegalDocument(document=document, title=_title_of(text, document),
                         version=version, content=text)
