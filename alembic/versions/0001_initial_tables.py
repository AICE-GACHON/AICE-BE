"""initial tables

Revision ID: 0001
Revises:
Create Date: 2026-07-21

이 마이그레이션은 논문 평가 및 피드백 서비스에 필요한 7개 테이블을
전부 생성합니다. 외래키(FK)가 참조하는 테이블이 먼저 만들어져야 하므로,
순서가 중요합니다.
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
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # 2. papers - OpenReview에서 수집한 기존 논문 코퍼스, 독립 테이블
    op.create_table(
        "papers",
        sa.Column("paper_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_id", sa.String(100), nullable=False, unique=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=False),
        sa.Column("venue", sa.String(100), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("field", sa.String(100)),
        sa.Column("pdf_url", sa.String(500)),
        # TODO: pgvector 확장 설치 후 sa.Text -> Vector(1536) 타입으로 교체
        sa.Column("embedding", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # 3. reviews (FK -> papers)
    op.create_table(
        "reviews",
        sa.Column("review_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("paper_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("papers.paper_id"), nullable=False),
        sa.Column("reviewer_label", sa.String(50), nullable=False),
        sa.Column("rating", sa.Integer()),
        sa.Column("confidence", sa.Integer()),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("decision", sa.String(30)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # 4. revisions (FK -> papers)
    op.create_table(
        "revisions",
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("paper_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("papers.paper_id"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("change_summary", sa.Text()),
        sa.Column("content", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # 5. submissions (FK -> users)
    op.create_table(
        "submissions",
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=False),
        sa.Column("content", sa.Text()),
        sa.Column("field", sa.String(100)),
        # TODO: pgvector 확장 설치 후 sa.Text -> Vector(1536) 타입으로 교체
        sa.Column("embedding", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # 6. similar_paper_matches (FK -> submissions, papers)
    op.create_table(
        "similar_paper_matches",
        sa.Column("match_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "submission_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("submissions.submission_id"),
            nullable=False,
        ),
        sa.Column("paper_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("papers.paper_id"), nullable=False),
        sa.Column("similarity_score", sa.Numeric(5, 4), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # 7. review_predictions (FK -> submissions)
    op.create_table(
        "review_predictions",
        sa.Column("prediction_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "submission_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("submissions.submission_id"),
            nullable=False,
        ),
        sa.Column("predicted_points", sa.Text(), nullable=False),
        sa.Column("suggested_revision", sa.Text()),
        sa.Column("based_on_matches", sa.JSON()),
        sa.Column("explanation_source", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )


def downgrade() -> None:
    # 만들 때의 역순으로 삭제 (FK 참조 관계 때문에 순서 중요)
    op.drop_table("review_predictions")
    op.drop_table("similar_paper_matches")
    op.drop_table("submissions")
    op.drop_table("revisions")
    op.drop_table("reviews")
    op.drop_table("papers")
    op.drop_table("users")
