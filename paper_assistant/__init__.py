"""paper_assistant — ML/AI 논문 리서치 어시스턴트 (AI 파트).

공개 API (백엔드 통합 계약):
    from paper_assistant import analyze, get_paper_detail, get_paper_revisions
    report = analyze(title, abstract)        # -> Report (Pydantic)
    detail = get_paper_detail(paper_id)      # -> PaperDetail | None
    revs = get_paper_revisions(paper_id)     # -> PaperRevisions | None (외부 API 호출)
"""


def analyze(*args, **kwargs):
    # 지연 import — 무거운 의존성(torch 등)을 실제 호출 시에만 로드
    from paper_assistant.graph.pipeline import analyze as _analyze
    return _analyze(*args, **kwargs)


def get_paper_detail(*args, **kwargs):
    """Report의 similar_papers[].paper_id로 원문·리뷰 전문을 조회한다.

    analyze()와 달리 DB 조회만 하므로 임베딩 모델을 로드하지 않는다.
    """
    from paper_assistant.detail import get_paper_detail as _detail
    return _detail(*args, **kwargs)


def list_papers(*args, **kwargs):
    """venue/year/field/q로 코퍼스 논문 목록을 조회한다. DB 조회만 하므로 가볍다."""
    from paper_assistant.detail import list_papers as _list
    return _list(*args, **kwargs)


def extract_pdf_title_abstract(*args, **kwargs):
    """PDF 바이트 -> (title, abstract). analyze(pdf_bytes=...)가 내부에서 쓰는 것과
    같은 추출기다 — 백엔드가 제목/초록만 필요할 때(POST /api/submissions/pdf) 전체
    analyze()를 돌리지 않고 이 함수만 쓴다.
    """
    from paper_assistant.pdf.extract import extract_title_abstract as _extract
    return _extract(*args, **kwargs)


def get_paper_revisions(*args, **kwargs):
    """저자가 리뷰를 받고 무엇을 고쳤는지 (제목·초록·PDF 변경 이력).

    DB에는 최신 버전만 남으므로 OpenReview API를 실시간 조회한다 — 느리고
    실패할 수 있으니 사용자가 명시적으로 요청했을 때만 호출할 것.
    """
    from paper_assistant.revisions import get_paper_revisions as _revs
    return _revs(*args, **kwargs)
