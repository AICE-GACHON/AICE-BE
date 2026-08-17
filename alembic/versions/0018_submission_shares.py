"""분석 결과의 공개 공유 링크 테이블 추가

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-17

결과 화면 경로가 로그인 필수라, 공유 링크를 받은 사람도 그 계정으로 로그인해야만
열 수 있었다. 소유자가 발급한 토큰 하나로 비로그인 열람을 허용한다 (이슈 #30).

**부분 유니크 인덱스가 이 마이그레이션의 핵심이다.** submission 하나에 살아 있는
(revoked_at IS NULL) 토큰을 하나로 묶는다. 애플리케이션에서 "있으면 재사용, 없으면
생성"으로만 막으면 동시 요청 두 건이 같은 순간에 조회를 통과해 둘 다 INSERT 할 수
있고, 그러면 DELETE가 그중 하나만 폐기해 **폐기했다고 응답한 링크가 계속 열린다.**
review_predictions_one_active(0002)와 같은 장치이고 같은 이유다.

token에 거는 unique는 인덱스를 겸하므로 조회용 인덱스를 따로 만들지 않는다 —
공개 조회는 항상 token 등가 비교 하나다.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "submission_shares",
        sa.Column("share_id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("submission_id", sa.UUID(as_uuid=True), nullable=False),
        # 128자는 secrets.token_urlsafe(32)의 43자에 여유를 둔 값이다.
        sa.Column("token", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        # 초안이 지워지면 그 공유 링크도 함께 사라져야 한다. 남아 있으면 공개
        # 조회가 없는 submission을 참조하게 된다.
        sa.ForeignKeyConstraint(
            ["submission_id"], ["submissions.submission_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token", name="submission_shares_token_key"),
    )
    op.create_index(
        "submission_shares_one_active", "submission_shares", ["submission_id"],
        unique=True, postgresql_where=sa.text("revoked_at IS NULL"))


def downgrade() -> None:
    op.drop_index("submission_shares_one_active", table_name="submission_shares")
    op.drop_table("submission_shares")
