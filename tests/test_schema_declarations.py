"""모델·alembic 설정이 실제 DB를 설명하는가 (DB 불필요).

**왜 이 파일이 있는가.** alembic autogenerate는 "모델에 없는 것"을 전부 삭제 대상으로
본다. 그래서 DB에는 있는데 모델/설정에 선언되지 않은 테이블·인덱스가 생기면, 다음에
누군가 `alembic revision --autogenerate`를 돌리는 순간 그것을 **DROP하는 마이그레이션이
조용히 만들어진다.** 실행 중에는 아무 증상이 없다가 마이그레이션을 적용하는 순간
터지는 종류의 문제다.

실제로 두 건이 그 상태였다 (2026-08-06 발견):
  - paper_stories: alembic 0009가 만들었는데 CORPUS_TABLES에 없어 DROP 대상이었다.
  - review_predictions_one_active: alembic 0002가 만든 중복 분석 방지 인덱스인데
    모델에 선언이 없어 DROP 대상이었다.

`alembic check`가 이 상태를 잡지만 DB와 마이그레이션이 필요하다. 아래 테스트는 소스만
읽으므로 어디서든 돌고, **드리프트가 생긴 그 커밋에서** 바로 실패한다.
"""
import ast
import re
from pathlib import Path

from app.models.analysis import IN_PROGRESS, ReviewPrediction

ROOT = Path(__file__).resolve().parents[1]
INIT_DB_SQL = ROOT / "scripts" / "init_db.sql"
ALEMBIC_ENV = ROOT / "alembic" / "env.py"


def _tables_in_init_db() -> set[str]:
    sql = INIT_DB_SQL.read_text(encoding="utf-8")
    return set(re.findall(r"^CREATE TABLE (\w+)", sql, re.MULTILINE))


def _corpus_tables() -> set[str]:
    """alembic/env.py에서 CORPUS_TABLES를 **파싱해서** 꺼낸다.

    import하지 않는 이유가 두 가지다. alembic/ 은 패키지가 아니라 마이그레이션
    디렉터리라 `alembic.env`가 설치된 alembic 패키지로 잡히고, 설령 경로로 불러와도
    env.py는 모듈 수준에서 실제 마이그레이션을 실행한다 — 테스트가 DB를 건드리게 된다.
    """
    tree = ast.parse(ALEMBIC_ENV.read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "CORPUS_TABLES" for t in node.targets)):
            return set(ast.literal_eval(node.value))
    raise AssertionError("alembic/env.py에서 CORPUS_TABLES를 찾지 못했다")


CORPUS_TABLES = _corpus_tables()


def test_every_corpus_table_is_excluded_from_autogenerate():
    """init_db.sql이 만드는 테이블은 전부 CORPUS_TABLES에 있어야 한다.

    빠뜨리면 autogenerate가 그 테이블을 DROP하는 마이그레이션을 만든다. 코퍼스
    테이블에는 SQLAlchemy 모델이 없으므로 alembic 입장에서는 고아로 보인다.
    """
    missing = _tables_in_init_db() - CORPUS_TABLES
    assert not missing, (
        f"init_db.sql이 만드는데 alembic/env.py의 CORPUS_TABLES에 없는 테이블: "
        f"{sorted(missing)} — autogenerate가 DROP을 제안하게 된다")


def test_corpus_table_list_has_no_stale_entries():
    """반대 방향 — 없어진 테이블이 목록에 남아 있으면 목록을 믿을 수 없게 된다."""
    stale = CORPUS_TABLES - _tables_in_init_db()
    assert not stale, f"init_db.sql에 없는데 CORPUS_TABLES에 남아 있음: {sorted(stale)}"


def test_active_analysis_guard_is_declared_on_the_model():
    """중복 분석 방지 인덱스가 모델에 선언돼 있어야 한다.

    이 인덱스를 실제로 만드는 것은 alembic 0002지만, 모델에 없으면 autogenerate가
    DROP을 제안한다. 그 마이그레이션이 적용되면 같은 논문에 분석이 동시에 두 번
    돌 수 있게 되는데, 라우터는 이 인덱스가 던지는 IntegrityError에 의존한다
    (app/routers/submissions.py의 start_analysis).
    """
    index = next(
        (ix for ix in ReviewPrediction.__table__.indexes
         if ix.name == "review_predictions_one_active"), None)
    assert index is not None, "review_predictions_one_active 선언이 모델에서 사라졌다"
    assert index.unique, "유니크가 아니면 중복을 막지 못한다"
    assert [c.name for c in index.columns] == ["submission_id"]


def test_guard_condition_follows_the_in_progress_constant():
    """인덱스 WHERE 절과 IN_PROGRESS가 따로 놀면 안 된다.

    상태를 하나 추가했는데(예: 'queued') WHERE 절을 안 고치면, 그 상태의 분석은
    중복 방지를 우회한다. 에러는 안 나고 그냥 두 번 돌아간다.
    """
    index = next(ix for ix in ReviewPrediction.__table__.indexes
                 if ix.name == "review_predictions_one_active")
    where = str(index.dialect_options["postgresql"]["where"])
    for status in IN_PROGRESS:
        assert f"'{status}'" in where, f"{status}가 인덱스 조건에서 빠졌다: {where}"
