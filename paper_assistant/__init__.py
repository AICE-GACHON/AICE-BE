"""paper_assistant — ML/AI 논문 리서치 어시스턴트 (AI 파트).

공개 API (백엔드 통합 계약).

    from paper_assistant import (
        analyze, get_paper_detail, get_paper_reviews, get_paper_revisions,
        get_paper_story, list_papers, extract_pdf_title_abstract)

    report  = analyze(title, abstract)      # -> Report (무겁다: 임베딩+검색+집계)
    detail  = get_paper_detail(paper_id)    # -> PaperDetail | None  (DB 조회만)
    reviews = get_paper_reviews(paper_id)   # -> list[ReviewDetail] | None
    revs    = get_paper_revisions(paper_id) # -> PaperRevisions | None (외부 API)
    body    = get_paper_revisions_with_body(paper_id)  # -> PaperRevisions | None (+ 본문 diff, 외부 API)
    story   = get_paper_story(paper_id)     # -> PaperStory | None (외부 API + LLM)
    listing = list_papers(...)              # -> PaperListResponse (DB 조회만)
    title, abstract = extract_pdf_title_abstract(pdf_bytes)  # PDF -> (title, abstract)
    warmup()                                # 기동 시 SPECTER2 선로드 (선택)

무거운 의존성(torch 등)을 서버 기동이 아니라 첫 호출 때 로드하기 위해 전부
지연 import한다. 타입 정보는 TYPE_CHECKING 블록으로 유지한다.

**백엔드(app/)가 들어올 수 있는 문은 이 세 개뿐이다.** 위 함수들과, 타입을 위한
`paper_assistant.schemas`, 공유 설정인 `paper_assistant.config`. 그 밖의 내부
모듈(graph/, retrieval/, llm, pdf/ ...)을 app/에서 직접 import하면 tests/meta의
경계 테스트가 실패한다 — LLM 인스턴스나 그래프 컴파일 같은 내부 결정이 백엔드로
새어 나가면, 여기를 고칠 때 app/까지 함께 깨진다.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # 런타임에는 임포트하지 않는다 (torch 로드 회피)
    from paper_assistant.schemas import (
        PaperDetail, PaperListResponse, PaperRevisions, PaperStory, Report,
        ReviewDetail)

__all__ = [
    "analyze",
    "warmup",
    "get_paper_detail",
    "get_paper_reviews",
    "get_paper_revisions",
    "get_paper_revisions_with_body",
    "get_paper_story",
    "list_papers",
    "extract_pdf_title_abstract",
    "pdf_page_count",
]


def analyze(title: str, abstract: str = "", pdf_bytes: bytes | None = None,
            use_llm: bool | None = None) -> "Report":
    """논문 초안 하나를 분석해 종합 Report를 만든다.

    use_llm=None이면 설정값(PAPER_ASSISTANT_USE_LLM)을 따른다. 첫 호출은 SPECTER2
    로드로 수십 초 걸리므로 요청-응답 안에서 동기로 부르지 말 것.
    """
    from paper_assistant.graph.pipeline import analyze as _fn
    return _fn(title, abstract, pdf_bytes=pdf_bytes, use_llm=use_llm)


def warmup(use_llm: bool | None = None) -> None:
    """SPECTER2와 그래프를 미리 로드한다 — 첫 analyze()의 수십 초를 앞당겨 치른다.

    서버 기동 시점(WARMUP_ON_STARTUP=1)에 부르라고 있는 것이다. 부르지 않아도
    동작은 같고, 그 비용이 첫 사용자 요청에 실릴 뿐이다.
    """
    from paper_assistant.graph.pipeline import warmup as _fn
    return _fn(use_llm)


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


def extract_pdf_title_abstract(pdf_bytes: bytes,
                               use_llm: bool | None = None) -> tuple[str, str]:
    """PDF 바이트 -> (title, abstract). analyze(pdf_bytes=...)가 내부에서 쓰는 것과
    같은 추출기다 — 백엔드가 제목/초록만 필요할 때(POST /api/submissions/pdf) 전체
    analyze()를 돌리지 않고 이 함수만 쓴다.

    use_llm=None이면 설정값(PAPER_ASSISTANT_USE_LLM)을 따른다. 켜져 있으면 텍스트
    정제에 Haiku를 한 번 부르고, 텍스트 레이어가 없는 스캔본이면 앞 2페이지를
    그림으로 렌더해 비전으로 읽는다. 꺼져 있으면 순수 텍스트 추출만 한다.

    **LLM 인스턴스는 호출자가 만들지 않는다.** analyze()와 마찬가지로 그 결정은
    paper_assistant.config 하나가 소유한다 — 호출자가 내부 모듈을 알 필요가 없다.
    """
    from paper_assistant.llm import get_llm
    from paper_assistant.pdf.extract import extract_title_abstract as _extract
    return _extract(pdf_bytes, llm=get_llm(use_llm))


def pdf_page_count(pdf_bytes: bytes) -> int:
    """PDF 페이지 수. 페이지 수 상한 검사를 **추출 전에** 하기 위한 것이다.

    extract_pdf_title_abstract()보다 훨씬 싸므로, 거부할 문서에 파싱 비용을 쓰지
    않으려면 이걸 먼저 부를 것.
    """
    from paper_assistant.pdf.extract import page_count as _fn
    return _fn(pdf_bytes)


def get_paper_revisions(paper_id: int) -> "PaperRevisions | None":
    """저자가 리뷰를 받고 무엇을 고쳤는지 (제목·초록·PDF 변경 이력).

    DB에는 최신 버전만 남으므로 OpenReview API를 실시간 조회한다 — 느리고
    실패할 수 있으니 사용자가 명시적으로 요청했을 때만 호출할 것.
    """
    from paper_assistant.query.revisions import get_paper_revisions as _fn
    return _fn(paper_id)


def get_paper_revisions_with_body(paper_id: int, refresh: bool = False) -> "PaperRevisions | None":
    """get_paper_revisions에 pdf 교체 지점마다 본문 전체 diff를 얹은 버전.

    LLM은 쓰지 않는다 — pdf가 바뀐 지점마다 전후 PDF를 실제로 내려받아 텍스트를
    뽑고(PyMuPDF) 단어 단위로 비교(difflib)하는 결정론적 계산이다. get_paper_revisions
    보다 훨씬 비싸므로(리비전마다 PDF 다운로드+파싱) 결과를 캐시한다 — 두 번째
    호출부터는 DB 조회 1번이다. refresh=True면 캐시를 무시하고 다시 만든다.
    """
    from paper_assistant.query.revisions import get_paper_revisions_with_body as _fn
    return _fn(paper_id, refresh=refresh)


def get_paper_story(paper_id: int, use_llm: bool | None = None,
                    refresh: bool = False) -> "PaperStory | None":
    """이 논문이 무엇을 지적받아 무엇을 고쳤는지 (재투고 궤적 + 심사 타임라인 + 요약).

    get_paper_detail(원문·리뷰)과 get_paper_revisions(수정 diff)를 시간축으로 엮은
    것이다. 둘을 각각 호출해 프론트에서 합칠 필요가 없다.

    가장 무거운 조회다 — OpenReview를 2번 타고 use_llm이면 Sonnet까지 부른다.
    대신 결과를 paper_stories에 캐시하므로 두 번째 호출부터는 DB 조회 1번이다.
    use_llm=None이면 설정값(PAPER_ASSISTANT_USE_LLM)을 따른다.
    """
    from paper_assistant.query.story import get_paper_story as _fn
    return _fn(paper_id, use_llm=use_llm, refresh=refresh)
