"""review_predictions에 분석 진행 상황(progress) 추가

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-15

분석은 수십 초~수 분 걸리는데 그동안 이 테이블이 아는 것은 status(pending|running)
뿐이었다. 그래서 화면도 "분석 중…" 한 줄밖에 쓸 수 없었다 — 서버가 정말 몰랐기
때문이지, 화면이 게을러서가 아니다.

이제 파이프라인이 단계마다 ProgressEvent를 내보내므로(paper_assistant.analyze의
on_event) 그것을 여기에 쌓는다. 화면은 폴링할 때마다 이 배열을 통째로 받는다.

**단일 값이 아니라 배열인 것이 핵심이다.** 프론트 폴링이 3초 간격이라 그보다 짧은
단계는 "지금 값"만 저장하면 화면에 뜨지도 못하고 지나간다. 배열이면 다음 폴링이
지나간 단계까지 함께 받으므로 아무 단계도 사라지지 않는다 — 폴링만으로도 SSE와
차이가 "전환이 최대 3초 늦게 보인다" 하나로 줄어든다. SSE를 나중에 얹더라도 이
컬럼은 그대로 쓴다(새로고침·재접속 때 처음부터 다시 그려야 하므로).

NOT NULL + server_default '[]' 로 둔다. nullable로 두면 None과 빈 배열이라는
같은 뜻의 두 상태가 생기고, 응답 스키마와 프론트 양쪽에 그 분기가 번진다.
기존 행은 이미 끝난 분석이라 빈 배열이 맞다(진행 기록이 없었다는 사실 그대로).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "review_predictions",
        sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    op.drop_column("review_predictions", "progress")
