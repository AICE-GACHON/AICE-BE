"""백엔드가 AI 파트의 **공개 API로만** 들어오는가 (DB 불필요).

**왜 이 파일이 있는가.** paper_assistant/__init__.py는 "공개 API는 함수 8개뿐"이라고
선언하지만, 파이썬은 그걸 강제하지 않는다. 필요한 것이 공개 API에 없으면 내부 모듈을
직접 import하는 쪽이 언제나 더 쉽고, 그렇게 한 줄씩 새는 동안 아무 증상이 없다.

실제로 두 건이 새어 있었다 (2026-08-07 발견):
  - app/routers/submissions.py가 graph.llm.get_llm을 직접 불러 LLM 인스턴스를
    만들었다. LLM 사용 여부는 paper_assistant.config가 소유한다는 규칙이 백엔드
    한 곳에서만 깨져 있었다.
  - app/main.py가 graph.pipeline.warmup을 직접 불렀다. DAG 파일을 옮기는 순간
    백엔드 기동이 깨지는 상태였다.

아래 테스트는 소스만 읽으므로 어디서든 돌고, **새는 그 커밋에서** 바로 실패한다.
"""
import ast
from pathlib import Path

import paper_assistant

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"

# 공개 함수 외에 app/이 만져도 되는 두 모듈.
#
#   schemas — 응답 DTO의 원본. app/schemas가 Report 등을 그대로 재수출하므로
#             타입 수준에서는 어차피 한 몸이다.
#   config  — DB·LLM 공유 설정의 단일 소스 (README "환경변수" 참고). app/core/config가
#             여기서 읽어 쓰는 것이 의도된 방향이다.
ALLOWED_SUBMODULES = {"schemas", "config"}


def _paper_assistant_imports() -> list[tuple[str, str]]:
    """app/ 전체에서 paper_assistant를 건드리는 import를 (파일, 대상)으로 모은다."""
    found: list[tuple[str, str]] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] == "paper_assistant":
                    for alias in node.names:
                        found.append((rel, f"{node.module}.{alias.name}"))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "paper_assistant":
                        found.append((rel, alias.name))
    return found


def test_app_only_touches_the_public_api():
    """app/이 import하는 것은 공개 함수이거나 허용된 두 모듈이어야 한다."""
    public = set(paper_assistant.__all__)
    leaks = []
    for rel, target in _paper_assistant_imports():
        parts = target.split(".")
        if len(parts) == 1:                             # import paper_assistant
            # 패키지를 통째로 들고 가면 내부 어디든 찌를 수 있으므로 누수로 본다.
            # (여기서 걸러두지 않으면 아래 parts[1]이 IndexError로 터져서, 무엇이
            #  문제인지 알려주지 못한 채 테스트만 죽는다.)
            leaks.append(f"{rel}: {target}")
            continue
        if len(parts) == 2 and parts[1] in public:      # from paper_assistant import analyze
            continue
        if parts[1] in ALLOWED_SUBMODULES:              # schemas / config
            continue
        leaks.append(f"{rel}: {target}")

    assert not leaks, (
        "app/이 paper_assistant 내부 모듈을 직접 import한다. 필요한 기능이면 "
        "paper_assistant/__init__.py에 공개 함수로 노출할 것:\n  " + "\n  ".join(leaks))


def test_public_api_names_are_actually_callable():
    """__all__에 이름만 올리고 함수를 안 만든 경우를 잡는다.

    지연 import 구조라 `from paper_assistant import 없는이름`이 ImportError로
    터지는 시점이 실행 중이다 — 배포 뒤에야 드러난다.
    """
    missing = [n for n in paper_assistant.__all__
               if not callable(getattr(paper_assistant, n, None))]
    assert not missing, f"__all__에 있으나 정의되지 않았다: {missing}"


def test_paper_assistant_never_imports_app():
    """의존 방향은 한쪽뿐이다 — app → paper_assistant.

    반대가 생기면 AI 파트를 백엔드 없이 돌릴 수 없게 되고(scripts/의 배치들이
    FastAPI 설정을 요구하게 된다), 순환 import도 시간문제다.
    """
    offenders = []
    for path in sorted((ROOT / "paper_assistant").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod = (node.module if isinstance(node, ast.ImportFrom)
                   else None)
            names = ([mod] if mod else
                     [a.name for a in getattr(node, "names", [])]
                     if isinstance(node, ast.Import) else [])
            if any(n and n.split(".")[0] == "app" for n in names):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, f"paper_assistant가 app을 import한다: {sorted(set(offenders))}"
