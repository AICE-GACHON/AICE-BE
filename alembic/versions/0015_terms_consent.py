"""users에 약관·개인정보처리방침 동의 이력 추가

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-16

약관(app/legal/)을 게시하면 "이 사람이 언제 어느 버전에 동의했는가"를 남겨야 한다.
동의를 받았다는 사실만 있고 **어느 문구에** 동의했는지가 없으면, 문서를 한 번이라도
개정한 뒤에는 아무것도 증명하지 못한다. 그래서 시각과 버전 두 가지를 함께 찍는다.

**세 컬럼 모두 nullable이고, 기본값을 채우지 않는다.** 여기가 이 마이그레이션의
유일한 판단 지점이다. `server_default='1.0'`으로 기존 행을 메우면 스키마는 깔끔해지지만,
그건 받은 적 없는 동의를 받았다고 기록하는 것이다 — 동의 이력을 남기는 목적 자체를
배반하고, 분쟁이 생기면 오히려 불리한 기록이 된다.

그래서 기존 계정은 null로 남는다. null인 계정은 User.consent_up_to_date가 False라
프론트가 다음 접속 때 재동의를 받는다. 즉 "빈 값"이 그대로 "재동의 필요"로 이어지고,
따로 마이그레이션할 것이 없다.

컬럼을 하나(terms_agreed_at)만 두지 않은 이유: 약관과 처리방침은 따로 개정된다.
처리방침만 고쳤을 때 약관 동의까지 무효로 만들면 불필요한 재동의를 받게 된다.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("terms_agreed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users", sa.Column("terms_version", sa.String(length=20), nullable=True))
    op.add_column(
        "users", sa.Column("privacy_version", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "privacy_version")
    op.drop_column("users", "terms_version")
    op.drop_column("users", "terms_agreed_at")
