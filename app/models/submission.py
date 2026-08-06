import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, func)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Submission(Base):
    """사용자가 직접 올린 '내 논문'(submissions) 테이블.

    코퍼스의 papers 테이블과 구조가 비슷해 보이지만 성격이 다릅니다 — papers는 이미
    심사가 끝난 공개 논문이고, submissions는 아직 리뷰를 받지 않은 사용자 소유의 논문입니다.

    임베딩 컬럼은 두지 않습니다. 분석할 때마다 paper_assistant가 SPECTER2로 계산하고
    버립니다 (저장하려면 vector(768)이어야 하는데, 재사용 이득보다 스키마 결합이 큽니다).

    **PDF 원본은 저장합니다** (alembic 0011). 분석은 BackgroundTasks라 요청이 끝난 뒤에
    실행되는데, 2단계 LLM 재정렬이 입력 논문의 본문·참고문헌까지 봐야 하기 때문입니다.
    업로드 시점에 저장해 두지 않으면 그때 가서 다시 구할 방법이 없습니다.
    """
    __tablename__ = "submissions"
    # 인덱스는 마이그레이션과 여기 양쪽에 있어야 한다. 모델에 없으면 alembic
    # autogenerate가 "모델에 없는 인덱스"로 보고 DROP을 제안한다.
    __table_args__ = (Index("submissions_user", "user_id", "created_at"),)

    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    abstract: Mapped[str] = mapped_column(Text, nullable=False)
    # 텍스트 붙여넣기 경로가 사라지면서 항상 NULL이 됩니다. 과거 초안의 내용을
    # 지우지 않으려고 컬럼은 남겨 뒀습니다 — 정리는 별도 작업입니다.
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    field: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # ⚠️ **deferred=True 를 지우지 마세요.** 최대 20MB짜리 블롭이라, 지연 로딩이 아니면
    # 목록 조회(GET /api/submissions)가 행마다 이걸 끌어옵니다. 에러가 나지 않고 그냥
    # 조용히 느려지는 종류의 문제입니다. 실제로 읽는 곳은 분석 실행 한 곳뿐입니다.
    pdf_bytes: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True, deferred=True)
    # 업로드 때 한 번 세어 둡니다 (경고·거부 판정마다 PDF를 다시 열지 않으려고).
    # 프론트의 15p 초과 경고와 서버의 60p 하드 거부가 이 값을 씁니다.
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
