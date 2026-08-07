"""논문 초안 도메인 — 업로드 검증·추출·저장과 소유권 경계.

라우터에서 옮겨온 이유는 **이 파일의 상수들이 서비스 정책이지 HTTP 규약이 아니기
때문이다.** 20MB 상한, 60페이지 거부선, 초록 2만자 — 전부 "무엇을 논문으로 받아줄
것인가"에 대한 결정이고, 그 결정은 나중에 배치 재처리나 관리자 도구에서도 똑같이
필요해진다. 라우터에 두면 HTTP 요청을 흉내내지 않고는 재사용할 수 없다.

라우터에 남는 것은 HTTP 관심사뿐이다: 경로·인증·rate limit·응답 모델·백그라운드
작업 등록.

**HTTPException을 여기서 던진다.** app/core/errors.py의 규약이 "라우터는 그냥
HTTPException을 raise한다"이고, 그 변환기는 서비스에서 올라온 것도 똑같이 처리한다.
도메인 예외 계층을 따로 두면 이 저장소에서는 매핑 코드만 늘어난다.
"""
import logging
import uuid

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.submission import Submission
from app.models.user import User
from paper_assistant import extract_pdf_title_abstract, pdf_page_count

log = logging.getLogger(__name__)

# 스캔본은 이제 비전 폴백이 살린다(extract.py). 여기까지 왔다는 것은 논문이 아니거나
# 비전도 읽지 못했다는 뜻이라, 원인을 스캔본으로 단정하지 않는다.
_PDF_EXTRACT_ERROR = (
    "PDF에서 제목과 초록을 추출할 수 없습니다. 논문 PDF가 맞는지 확인해 주세요.")

# Form 파라미터는 Pydantic Field(max_length=...) 검증을 안 타서 DB 컬럼 길이
# (String(300)/String(100))를 넘기면 500이 난다. 여기서 미리 걸러 422로 통일한다.
MAX_TITLE_LEN = 300
MAX_FIELD_LEN = 100
MAX_ABSTRACT_LEN = 20_000  # Text 컬럼이라 DB 상한이 없다 — 여기서만 막힌다
MAX_PDF_BYTES = 20 * 1024 * 1024  # 20MB — 논문 PDF치고 넉넉한 상한. 메모리에 통째로 읽으므로 무제한은 위험하다.
_PDF_CHUNK = 1024 * 1024          # 상한 검사를 위해 나눠 읽는 단위

# 페이지 상한이 둘인 이유: 경고와 거부는 다른 일이다.
#
# LLM 재정렬은 PDF를 통째로 넘기고 페이지당 약 2,970토큰이 든다. 60p면 분석 1회에
# 약 $0.36이라, 그 위는 논문이 아닐 개연성이 훨씬 높다고 보고 거부한다.
#
# WARN(15p)은 **서버가 강제하지 않는다.** page_count를 응답에 실어 보내면 프론트가
# "논문 PDF가 맞는지 확인해 주세요"를 띄우고, 사용자가 확인하면 그대로 진행한다.
# 경고는 UX이고 거부는 안전장치라, 한 곳에서 처리하려 하면 둘 다 어정쩡해진다.
WARN_PAGE_COUNT = 15
MAX_PAGE_COUNT = 60


def owned_submission(db: Session, submission_id: uuid.UUID, user: User) -> Submission:
    """내 초안일 때만 돌려준다. 남의 초안은 존재 여부도 알리지 않도록 404로 통일."""
    submission = db.get(Submission, submission_id)
    if submission is None or submission.user_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="논문 초안을 찾을 수 없습니다.")
    return submission


async def read_capped(upload: UploadFile, max_bytes: int = MAX_PDF_BYTES) -> bytes:
    """상한까지만 읽고, 넘으면 즉시 끊는다.

    예전에는 `await upload.read()`로 통째로 읽은 **뒤에** 길이를 봤다. 그러면
    거부할 파일도 일단 전부 메모리에 올라간다 — 로그인한 사용자 한 명이 수 GB짜리
    본문을 보내면 상한 검사에 도달하기 전에 서버가 죽는다. 검사가 읽기보다
    먼저여야 의미가 있다.

    한 청크를 더 읽어보는 이유: 정확히 max_bytes에서 멈추면 "딱 상한"과 "상한
    초과"를 구분할 수 없다.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_PDF_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="PDF 용량이 너무 큽니다 (20MB 초과).")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_form(title: str, abstract: str, field: str | None) -> None:
    if len(title) > MAX_TITLE_LEN or (field is not None and len(field) > MAX_FIELD_LEN):
        raise HTTPException(status_code=422, detail="title/field 길이가 너무 깁니다.")

    # abstract는 Text 컬럼이라 DB가 막아주지 않는다. 상한이 없으면 초록 자리에
    # 수 MB짜리 문자열을 넣어 행을 부풀릴 수 있고, 그 값은 그대로 임베딩·LLM
    # 입력으로 흘러간다. 논문 초록은 길어야 수천 자다.
    if len(abstract) > MAX_ABSTRACT_LEN:
        raise HTTPException(status_code=422, detail="abstract 길이가 너무 깁니다.")


def _count_pages(pdf_bytes: bytes) -> int:
    """페이지 수를 **추출보다 먼저** 센다 — 거부할 문서에 파싱 비용을 쓰지 않는다.

    여기서 PDF가 열리지 않으면 손상된 파일이므로 추출도 어차피 실패한다.
    """
    try:
        pages = pdf_page_count(pdf_bytes)
    except Exception:
        log.exception("PDF 열기 실패")
        raise HTTPException(status_code=422,
                            detail="PDF 파일을 열 수 없습니다. 손상된 파일일 수 있습니다.")

    if pages > MAX_PAGE_COUNT:
        raise HTTPException(
            status_code=422,
            detail=f"{pages}페이지 문서입니다. 논문 PDF만 분석할 수 있습니다 "
                   f"({MAX_PAGE_COUNT}페이지 이하).")
    return pages


def _fill_from_pdf(pdf_bytes: bytes, title: str, abstract: str) -> tuple[str, str]:
    """비어 있는 제목·초록만 PDF에서 채운다. 사용자가 준 값이 항상 우선한다."""
    if title and abstract:
        return title, abstract

    try:
        # use_llm은 넘기지 않는다: run_analysis와 같은 이유로 LLM 사용 여부는
        # paper_assistant.config 하나가 소유한다. 켜져 있으면 스캔본을 비전으로
        # 살리고(extract.py), 꺼져 있으면 순수 텍스트 추출만 한다.
        extracted_title, extracted_abstract = extract_pdf_title_abstract(pdf_bytes)
    except Exception:
        log.exception("PDF 추출 실패")
        raise HTTPException(status_code=422, detail=_PDF_EXTRACT_ERROR)

    # 추출값도 상한을 넘을 수 있다(조판이 이상한 PDF에서 초록 경계를 놓치면
    # 본문 전체가 딸려온다). 사용자가 보낸 값이 아니라 우리 추출기의 실수이므로
    # 거부하지 말고 자른다 — 임베딩은 어차피 앞부분만 쓴다.
    return (title or extracted_title[:MAX_TITLE_LEN],
            abstract or extracted_abstract[:MAX_ABSTRACT_LEN])


async def create_from_pdf(db: Session, user: User, pdf: UploadFile, *,
                          title: str = "", abstract: str = "",
                          field: str | None = None) -> Submission:
    """업로드된 PDF를 검증하고 초안 한 건으로 저장한다.

    순서에 비용 근거가 있다: 형식 → 폼 길이 → 본문 읽기(상한) → 페이지 수 →
    추출. 뒤로 갈수록 비싸므로 싼 검사에서 최대한 거른다.

    ⚠️ **추출에 실패하면 422로 거부한다.** 제목·초록이 없으면 1단계 임베딩 자체가
    불가능하다. 사용자가 고칠 기회를 주기보다 명확히 거부하는 쪽을 택했다.
    """
    is_pdf = (pdf.content_type == "application/pdf"
              or (pdf.filename or "").lower().endswith(".pdf"))
    if not is_pdf:
        raise HTTPException(status_code=422, detail="PDF 파일만 업로드할 수 있습니다.")

    _validate_form(title, abstract, field)

    pdf_bytes = await read_capped(pdf)
    pages = _count_pages(pdf_bytes)
    title, abstract = _fill_from_pdf(pdf_bytes, title, abstract)

    if not title or not abstract:
        raise HTTPException(status_code=422, detail=_PDF_EXTRACT_ERROR)

    submission = Submission(
        user_id=user.user_id, title=title, abstract=abstract, content=None,
        field=field, pdf_bytes=pdf_bytes, page_count=pages)
    db.add(submission)
    db.commit()
    db.refresh(submission)
    return submission


def list_for_user(db: Session, user: User) -> list[Submission]:
    """내 초안 목록 (최신순)."""
    # created_at이 같을 수 있다 (server_default now()는 트랜잭션 시각이라 한
    # 트랜잭션에서 만든 행끼리는 동률이다). submission_id로 순서를 확정한다.
    return list(db.scalars(
        select(Submission)
        .where(Submission.user_id == user.user_id)
        .order_by(Submission.created_at.desc(), Submission.submission_id.desc())
    ).all())


def delete(db: Session, submission: Submission) -> None:
    """초안과 그에 딸린 분석 결과를 함께 지운다 (FK ON DELETE CASCADE)."""
    db.delete(submission)
    db.commit()
