"""paper_assistant — ML/AI 논문 리서치 어시스턴트 (AI 파트).

공개 API (백엔드 통합 계약).

    from paper_assistant import (
        analyze, get_paper_detail, get_paper_reviews, get_paper_revisions,
        list_papers, extract_pdf_title_abstract)

    report  = analyze(title, abstract)      # -> Report (무겁다: 임베딩+검색+집계)
    detail  = get_paper_detail(paper_id)    # -> PaperDetail | None  (DB 조회만)
    reviews = get_paper_reviews(paper_id)   # -> list[ReviewDetail] | None
    revs    = get_paper_revisions(paper_id) # -> PaperRevisions | None (외부 API)
    listing = list_papers(...)              # -> PaperListResponse (DB 조회만)
    title, abstract = extract_pdf_title_abstract(pdf_bytes)  # PDF -> (title, abstract)

무거운 의존성(torch 등)을 서버 기동이 아니라 첫 호출 때 로드하기 위해 전부
지연 import한다. 타입 정보는 TYPE_CHECKING 블록으로 유지한다.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # 런타임에는 임포트하지 않는다 (torch 로드 회피)
    from paper_assistant.schemas import (
        PaperDetail, PaperListResponse, PaperRevisions, Report, ReviewDetail)

__all__ = [
    "analyze",
    "get_paper_detail",
    "get_paper_reviews",
    "get_paper_revisions",
    "list_papers",
    "extract_pdf_title_abstract",
]


def analyze(title: str, abstract: str = "", pdf_bytes: bytes | None = None,
            use_llm: bool | None = None) -> "Report":
    """논문 초안 하나를 분석해 종합 Report를 만든다.

    use_llm=None이면 설정값(PAPER_ASSISTANT_USE_LLM)을 따른다. 첫 호출은 SPECTER2
    로드로 수십 초 걸리므로 요청-응답 안에서 동기로 부르지 말 것.
    """
    from paper_assistant.graph.pipeline import analyze as _fn
    return _fn(title, abstract, pdf_bytes=pdf_bytes, use_llm=use_llm)


def get_paper_detail(paper_id: int) -> "PaperDetail | None":
    """Report의 similar_papers[].paper_id로 원문·리뷰 전문을 조회한다.

    analyze()와 달리 DB 조회만 하므로 임베딩 모델을 로드하지 않는다.
    """
    from paper_assistant.query.detail import get_paper_detail as _fn
    return _fn(paper_id)


def get_paper_reviews(paper_id: int) -> "list[ReviewDetail] | None":
    """논문 1편이 받은 리뷰만 조회한다 (점수 높은 순).

    상세 전체가 필요 없는 화면용 — 저자·지적항목 조회를 건너뛴다.
    논문 자체가 없으면 None, 논문은 있는데 리뷰가 없으면 빈 리스트다.
    """
    from paper_assistant.query.detail import get_paper_reviews as _fn
    return _fn(paper_id)


def list_papers(*args, **kwargs) -> "PaperListResponse":
    """venue/year/field/q로 코퍼스 논문 목록을 조회한다. DB 조회만 하므로 가볍다."""
    from paper_assistant.query.detail import list_papers as _fn
    return _fn(*args, **kwargs)


def extract_pdf_title_abstract(*args, **kwargs):
    """PDF 바이트 -> (title, abstract). analyze(pdf_bytes=...)가 내부에서 쓰는 것과
    같은 추출기다 — 백엔드가 제목/초록만 필요할 때(POST /api/submissions/pdf) 전체
    analyze()를 돌리지 않고 이 함수만 쓴다.
    """
    from paper_assistant.pdf.extract import extract_title_abstract as _extract
    return _extract(*args, **kwargs)


def get_paper_revisions(paper_id: int) -> "PaperRevisions | None":
    """저자가 리뷰를 받고 무엇을 고쳤는지 (제목·초록·PDF 변경 이력).

    DB에는 최신 버전만 남으므로 OpenReview API를 실시간 조회한다 — 느리고
    실패할 수 있으니 사용자가 명시적으로 요청했을 때만 호출할 것.
    """
    from paper_assistant.query.revisions import get_paper_revisions as _fn
    return _fn(paper_id)
