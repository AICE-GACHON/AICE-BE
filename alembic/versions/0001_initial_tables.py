"""initial tables

Revision ID: 0001
Revises:
Create Date: 2026-07-13

이 마이그레이션은 Data 모델링(Notion) 물리 설계 문서에 정의된
10개 테이블을 전부 생성합니다. 외래키(FK)가 참조하는 테이블이
먼저 만들어져야 하므로, 순서가 중요합니다.
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
        sa.Column("phone_number", sa.String(20), nullable=False, unique=True),
        sa.Column("nickname", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # 2. cards - users 참조 없음, 독립 테이블
    op.create_table(
        "cards",
        sa.Column("card_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("issuer", sa.String(50), nullable=False),
        sa.Column("annual_fee", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("highlight", sa.String(100)),
    )

    # 3. category_mappings - merchants가 참조하므로 merchants보다 먼저
    op.create_table(
        "category_mappings",
        sa.Column("kakao_category_code", sa.String(20), primary_key=True),
        sa.Column("internal_category_code", sa.String(20), nullable=False),
        sa.Column("mapping_type", sa.String(20), nullable=False),
    )

    # 4. auth_credentials (FK -> users)
    op.create_table(
        "auth_credentials",
        sa.Column("credential_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("login_type", sa.String(20), nullable=False),
        sa.Column("email", sa.String(100), unique=True),
        sa.Column("password_hash", sa.String(500)),
        sa.Column("kakao_user_id", sa.String(100), unique=True),
        sa.Column("refresh_token", sa.String(500)),
        sa.Column("expires_at", sa.DateTime()),
    )

    # 5. user_consents (FK -> users)
    op.create_table(
        "user_consents",
        sa.Column("consent_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("terms_version", sa.String(20), nullable=False),
        sa.Column("privacy_version", sa.String(20), nullable=False),
        sa.Column("agreed_at", sa.DateTime(), nullable=False),
    )

    # 6. user_cards (FK -> users, cards)
    op.create_table(
        "user_cards",
        sa.Column("user_card_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("card_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cards.card_id"), nullable=False),
        sa.Column("registered_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "card_id", name="uq_user_card"),
    )

    # 7. codef_connections (FK -> users)
    op.create_table(
        "codef_connections",
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("connected_id", sa.String(100), nullable=False),
        sa.Column("issuer_code", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("connected_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # 8. card_performances (FK -> user_cards)
    op.create_table(
        "card_performances",
        sa.Column("performance_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_card_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_cards.user_card_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("month_spend_amount", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_type", sa.String(20), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # 9. merchants (FK -> category_mappings)
    op.create_table(
        "merchants",
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("latitude", sa.Numeric(10, 7), nullable=False),
        sa.Column("longitude", sa.Numeric(10, 7), nullable=False),
        sa.Column(
            "kakao_category_code",
            sa.String(20),
            sa.ForeignKey("category_mappings.kakao_category_code"),
            nullable=False,
        ),
    )

    # 10. benefit_clauses (FK -> cards)
    op.create_table(
        "benefit_clauses",
        sa.Column("benefit_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("card_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cards.card_id"), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("benefit_type", sa.String(20), nullable=False),
        sa.Column("rate", sa.Numeric(5, 2), nullable=False),
        sa.Column("monthly_cap", sa.Integer()),
        sa.Column("min_spend", sa.Integer(), server_default="0"),
        sa.Column("include_notes", sa.String(500)),
        sa.Column("exclude_notes", sa.String(500)),
        # TODO: pgvector 확장 설치 후 sa.String -> Vector(1024) 타입으로 교체
        sa.Column("embedding", sa.String()),
    )

    # 11. recommendations (FK -> users, merchants, cards)
    op.create_table(
        "recommendations",
        sa.Column("recommendation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column(
            "merchant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("merchants.merchant_id"), nullable=False
        ),
        sa.Column("card_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cards.card_id"), nullable=False),
        sa.Column("expected_benefit_won", sa.Integer(), nullable=False),
        sa.Column("eligible", sa.Boolean(), server_default="true"),
        sa.Column("reason", sa.String(500)),
        sa.Column("caveats", sa.JSON()),
        sa.Column("explanation_source", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )


def downgrade() -> None:
    # 만들 때의 역순으로 삭제 (FK 참조 관계 때문에 순서 중요)
    op.drop_table("recommendations")
    op.drop_table("benefit_clauses")
    op.drop_table("merchants")
    op.drop_table("card_performances")
    op.drop_table("codef_connections")
    op.drop_table("user_cards")
    op.drop_table("user_consents")
    op.drop_table("auth_credentials")
    op.drop_table("category_mappings")
    op.drop_table("cards")
    op.drop_table("users")
