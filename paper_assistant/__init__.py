"""paper_assistant — ML/AI 논문 리서치 어시스턴트 (AI 파트).

공개 API (백엔드 통합 계약):
    from paper_assistant import analyze, get_paper_detail
    report = analyze(title, abstract)        # -> Report (Pydantic)
    detail = get_paper_detail(paper_id)      # -> PaperDetail | None
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
