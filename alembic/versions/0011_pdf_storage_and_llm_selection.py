"""submissions에 PDF 원본 저장 + similar_paper_matches에 LLM 선정 결과

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-06

유사 논문 선정을 2단계로 바꾸면서(docs/추천_파이프라인_재설계.md) 생긴 두 가지 요구.

**1. PDF 원본을 저장해야 한다.**
지금까지 PDF는 제목·초록만 뽑고 버렸다. 2단계 LLM 재정렬은 입력 논문의 본문과
참고문헌까지 봐야 하므로(그게 임베딩보다 정확한 이유다) 분석 시점에 PDF 바이트가
있어야 한다. 그런데 분석은 BackgroundTasks라 요청이 끝난 뒤에 실행된다 — 업로드
시점에 저장해 두지 않으면 다시 구할 방법이 없다.

BYTEA로 DB에 넣는다. 외부 스토리지나 Files API를 쓰지 않은 이유는 의존성이 늘고
수명 관리·정리 배치가 따라붙는 데 비해, 지금 규모에서 얻는 게 없기 때문이다.
사용자가 초안을 지우면 ON DELETE CASCADE로 같이 사라지는 것도 이 방식의 이점이다.

⚠️ **pdf_bytes 는 반드시 지연 로딩(deferred)으로 매핑할 것** (app/models/submission.py).
목록 조회가 20MB 블롭을 행마다 끌어오면 조용히 느려진다.

page_count 는 두 곳에서 쓴다 — 프론트의 "15p 초과" 경고와 서버의 60p 하드 거부.
업로드 때 한 번 세고 저장한다(PDF를 다시 열지 않기 위해).

**2. 검색 후보 전부를 남긴다.**
지금은 분석이 근거로 쓴 논문만 rank와 함께 남겼다. 2단계 구조에서는 "검색이 뽑은
후보 50편"과 "LLM이 고른 5편"이 다르고, **둘의 관계가 이 파이프라인의 핵심 품질
지표**다. LLM이 검색 상위에서만 고른다면 재정렬이 값을 못 하는 것이고, 하위에서
자주 고른다면 1단계 랭킹이 틀린 것이다.

그래서 후보 50편을 전부 남기고 selected 로 구분한다. 실사용 분석이 그대로 평가
데이터가 된다 — 유사도에는 사람 라벨 없는 정답지가 없으므로(재설계 문서 §8) 이게
현실적으로 확보 가능한 유일한 대규모 신호다. 분석 1회당 50행(수 KB)이면 싸다.

기존 컬럼 rank 의 **의미가 바뀐다**: 예전에는 최종 결과 순위였고 이제는 검색 순위다.
LLM 선정 순위는 llm_rank 로 따로 받는다.

0008·0010과 같은 방어 패턴은 쓰지 않는다 — 여기서 건드리는 두 테이블은 코퍼스가
아니라 alembic이 만든 서비스 테이블이라 항상 존재한다.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- 1. PDF 원본 ------------------------------------------------------
    # nullable: 개편 이전에 만들어진 초안에는 PDF가 없다. 그 행들을 지우지 않고
    # 남겨 두되, 분석을 걸면 "PDF가 없습니다"로 실패하게 된다.
    op.add_column("submissions", sa.Column("pdf_bytes", sa.LargeBinary(), nullable=True))
    op.add_column("submissions", sa.Column("page_count", sa.Integer(), nullable=True))

    # --- 2. LLM 선정 결과 --------------------------------------------------
    op.add_column(
        "similar_paper_matches",
        sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column(
        "similar_paper_matches", sa.Column("llm_rank", sa.Integer(), nullable=True))
    op.add_column(
        "similar_paper_matches", sa.Column("selection_reason", sa.Text(), nullable=True))

    # 선정된 5편만 꺼내는 것이 가장 흔한 조회다. 후보 50편이 쌓이는 테이블에서
    # 전체 인덱스를 태우지 않도록 부분 인덱스로 만든다.
    op.create_index(
        "similar_paper_matches_selected",
        "similar_paper_matches",
        ["prediction_id", "llm_rank"],
        postgresql_where=sa.text("selected"),
    )

    # 기존 행은 전부 '분석이 근거로 삼은 논문'이었으므로 selected=true 가 맞다.
    # 검색 순위가 곧 결과 순위였으니 llm_rank 도 rank 를 그대로 물려준다.
    op.execute("UPDATE similar_paper_matches SET selected = true, llm_rank = rank")


def downgrade() -> None:
    op.drop_index("similar_paper_matches_selected", table_name="similar_paper_matches")
    op.drop_column("similar_paper_matches", "selection_reason")
    op.drop_column("similar_paper_matches", "llm_rank")
    op.drop_column("similar_paper_matches", "selected")
    op.drop_column("submissions", "page_count")
    op.drop_column("submissions", "pdf_bytes")
