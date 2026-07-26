"""initial tables

Revision ID: 0001
Revises:
Create Date: 2026-07-21 (2026-07-26 AI 파트 통합으로 재작성)

이 마이그레이션은 **서비스 테이블만** 만듭니다.

논문 코퍼스(papers / authors / reviews / review_points / venue_stats / …)는 여기서
만들지 않습니다. AI 파트가 `scripts/init_db.sql`로 이미 만들어 43,515편을 적재해 둔
테이블이고, pgvector·tsvector·GIN 인덱스처럼 alembic으로 표현하기 번거로운 것들이
들어 있습니다. 같은 이름의 테이블을 alembic이 또 만들려 하면 충돌합니다.

통합 전 버전과 달라진 점 (전부 의도된 변경):
  - papers / reviews / revisions 테이블 제거
      papers·reviews는 코퍼스 쪽이 정본입니다.
      revisions("리뷰 이후 어떻게 수정됐는지")는 저장하지 않습니다 — papers는
      openreview_id로 upsert해서 최신 버전만 남기 때문에 과거 버전을 담을 수가 없고,
      대신 GET /api/papers/{id}/revisions가 OpenReview API를 실시간 조회합니다
      (paper_assistant.get_paper_revisions). 학회를 옮겨 다시 낸 흐름
      (ICLR reject → NeurIPS accept)은 그와 별개로 코퍼스의 submission_links로
      추적해 분석 결과의 resubmission_flows로 내려줍니다.
  - similar_paper_matches.similarity_score 제거
      논문별 유사도 점수는 만들 수 없습니다 (검색 상위 20편의 코사인 폭이 0.013이라
      1위와 20위가 사실상 같은 값). rank + match_type으로 대체했습니다.
  - review_predictions를 Report 저장용으로 재설계
      predicted_points TEXT 하나로는 분석 결과(신뢰도·lift·학회 경향·점수 분포)를
      담을 수 없어 report JSONB로 통째로 저장합니다. 분석이 백그라운드로 돌기 때문에
      status/error 컬럼도 함께 둡니다.
  - submissions.embedding 제거
      임베딩은 분석 시점에 계산하고 저장하지 않습니다. 저장하려면 vector(768)이어야
      하는데 TEXT로 두면 벡터 연산에 쓸 수 없습니다.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. users - 다른 테이블들이 참조하는 기준 테이블이라 가장 먼저 생성
    op.create_table(
        "users",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(100), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(500), nullable=False),
        sa.Column("nickname", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )

    # 2. submissions - 사용자가 올린 내 논문 초안 (FK -> users)
    op.create_table(
        "submissions",
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=False),
        sa.Column("content", sa.Text()),
        sa.Column("field", sa.String(100)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("submissions_user", "submissions", ["user_id"])

    # 3. review_predictions - 분석 1회분. 백그라운드 작업의 상태이자 결과 저장소.
    op.create_table(
        "review_predictions",
        sa.Column("prediction_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "submission_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("submissions.submission_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # pending -> running -> done | failed
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        # paper_assistant.schemas.Report 전체. AI 파트가 Report 필드를 늘려도
        # 마이그레이션 없이 그대로 담긴다.
        sa.Column("report", postgresql.JSONB()),
        # 아래 둘은 report 안에도 있지만, 목록 조회에서 매번 JSONB를 파싱하지 않도록
        # 꺼내 둔 사본이다. strong | moderate | weak
        sa.Column("confidence_level", sa.String(20)),
        sa.Column("is_reliable", sa.Boolean()),
        # "이 결과가 어떻게 만들어졌는지"를 항상 응답에 붙이기 위한 값.
        # stub = LLM 없이 규칙/통계만 (기본, $0), llm = Haiku/Sonnet 실제 호출
        sa.Column("explanation_source", sa.String(20), nullable=False, server_default="stub"),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
    )
    op.create_index(
        "review_predictions_submission",
        "review_predictions",
        ["submission_id", "created_at"],
    )

    # 4. similar_paper_matches - 이 분석이 근거로 삼은 유사 논문 목록.
    #    "이 예측이 어떤 논문을 보고 나왔는지"를 역방향으로도 조회할 수 있게 남긴다.
    op.create_table(
        "similar_paper_matches",
        sa.Column("match_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "prediction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("review_predictions.prediction_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # 코퍼스 papers.id (BIGINT). FK 제약은 걸지 않는다 — 코퍼스 테이블은
        # init_db.sql이 관리해서 이 마이그레이션 시점에 존재를 보장할 수 없다.
        sa.Column("paper_id", sa.BigInteger(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        # both(의미+용어) | semantic(의미만) | lexical(용어만) — 왜 걸렸는지의 근거
        sa.Column("match_type", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "similar_paper_matches_prediction", "similar_paper_matches", ["prediction_id", "rank"]
    )
    op.create_index("similar_paper_matches_paper", "similar_paper_matches", ["paper_id"])


def downgrade() -> None:
    # 만들 때의 역순으로 삭제 (FK 참조 관계 때문에 순서 중요)
    op.drop_table("similar_paper_matches")
    op.drop_table("review_predictions")
    op.drop_table("submissions")
    op.drop_table("users")
