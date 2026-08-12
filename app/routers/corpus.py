"""논문 코퍼스 조회 (OpenReview에서 수집한 기존 논문·리뷰·수정 이력·심사 서사).

코퍼스 테이블은 AI 파트가 소유하므로 SQLAlchemy가 아니라 paper_assistant의 공개
함수로 조회합니다. 어느 엔드포인트도 임베딩 모델(SPECTER2)을 쓰지 않습니다 —
무거운 건 분석(POST /api/submissions/{id}/analysis) 쪽뿐입니다.

다만 `/revisions`와 `/story`는 **외부 네트워크(OpenReview API)를 탑니다.** papers
테이블에 최신 버전만 남기 때문이고, `/story`는 여기에 LLM 요약까지 붙습니다.
둘 다 사용자가 명시적으로 눌렀을 때만 호출하세요.

⚠️ paper_id는 UUID가 아니라 BIGINT입니다. 분석 결과의
similar_papers[].paper_id를 그대로 넘기면 됩니다.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.core.deps import get_current_user_optional
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.common import ApiResponse
from app.schemas.corpus import (
    PaperListResponse, PaperResponse, ReviewResponse, RevisionsResponse,
    StoryResponse)
from paper_assistant import (
    get_paper_detail, get_paper_reviews, get_paper_revisions,
    get_paper_revisions_with_body, get_paper_story, list_papers as _list_papers)

router = APIRouter(prefix="/api/papers", tags=["papers"])

_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="논문을 찾을 수 없습니다.")

# `/revisions`와 `/story`만 외부 자원을 쓴다 — OpenReview 2콜, `/story`는 LLM까지.
# 나머지 조회는 DB만 보므로 제한하지 않는다.
#
# **왜 인증이 아니라 rate limit인가**: 랜딩의 데모가 비로그인 상태에서 `/story`를
# 부른다. 인증을 걸면 그 화면이 죽는다. 그리고 캐시된 논문을 다시 읽는 것은 DB 조회
# 1번이라 비용이 없다 — 막아야 할 것은 "조회"가 아니라 **캐시에 없는 논문을 연달아
# 훑는 것**이다. 시간당 상한이 그걸 정확히 끊는다.
#
# 30/hour 근거: 분석 1회가 논문 5편을 내놓으므로 한 사람이 전부 열어봐도 5회다.
# 6번 분석해야 걸린다. 반면 paper_id를 1부터 훑는 스캔은 30회에서 멈춘다.
#
# ⚠️ 저장소가 메모리라 워커를 늘리면 워커별로 따로 센다 (app/core/rate_limit.py).
# 배포에서 워커를 늘릴 거면 Redis 저장소로 바꿔야 이 상한이 실제로 상한이 된다.
_EXTERNAL_CALL_LIMIT = "30/hour"

# `/revisions/body-diff`는 그 위 두 엔드포인트보다 훨씬 무겁다 — 캐시 미스 1건이
# pdf가 교체된 지점마다 실제 PDF 다운로드(중복 제거되지만 각각 수 MB) + PyMuPDF
# 파싱 + 전체 본문 diff를 수행한다. 분석 1회가 논문 5편을 내놓는 기준으로 "분석
# 결과를 전부 열람"은 여전히 여유 있게 커버하면서, paper_id 스캔형 공격은
# `/revisions`(30회)의 3분의 1인 10회에서 끊는다.
_BODY_DIFF_CALL_LIMIT = "10/hour"


@router.get("", response_model=ApiResponse[PaperListResponse])
def list_papers(
    venue: str | None = None,
    year: int | None = None,
    field: str | None = Query(default=None, description="papers.primary_area"),
    q: str | None = Query(default=None, description="제목·초록 전문검색"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """코퍼스 논문 목록. venue/year/field/q로 좁힐 수 있다 (전부 선택)."""
    result = _list_papers(venue=venue, year=year, field=field, q=q, limit=limit, offset=offset)
    return ApiResponse[PaperListResponse](data=result)


@router.get("/{paper_id}", response_model=ApiResponse[PaperResponse])
def get_paper(paper_id: int):
    """유사 논문 1편의 상세 — 초록·저자·메타리뷰·리뷰 전문·지적 항목."""
    detail = get_paper_detail(paper_id)
    if detail is None:
        raise _NOT_FOUND
    return ApiResponse[PaperResponse](data=detail)


@router.get("/{paper_id}/reviews", response_model=ApiResponse[list[ReviewResponse]])
def list_paper_reviews(paper_id: int):
    """이 논문이 받은 리뷰 목록 (점수 높은 순, 점수 없는 리뷰는 뒤로).

    GET /api/papers/{paper_id} 응답에도 같은 리뷰가 들어 있습니다. 리뷰만 필요한
    화면을 위해 저자·지적항목 조회를 건너뛰는 가벼운 경로입니다.

    ⚠️ 2023년 이전 학회 리뷰는 강점/약점이 분리되지 않은 형식이라 weaknesses에 리뷰
    본문 전체가 들어옵니다. 이때 is_unsplit=true이므로, 프론트는 '약점'이라고 라벨을
    붙이지 말고 '리뷰 본문' 한 덩어리로 표시해야 합니다.
    """
    reviews = get_paper_reviews(paper_id)
    if reviews is None:
        raise _NOT_FOUND
    return ApiResponse[list[ReviewResponse]](data=reviews)


@router.get("/{paper_id}/revisions", response_model=ApiResponse[RevisionsResponse])
@limiter.limit(_EXTERNAL_CALL_LIMIT)
def get_revisions(request: Request, paper_id: int):
    """저자가 리뷰를 받고 무엇을 고쳤는지 — 제목·초록·PDF 변경 이력.

    ⚠️ IP 기준 시간당 30회로 제한됩니다 (외부 API를 타기 때문). 429가 오면
    잠시 뒤 다시 시도하세요.

    ⚠️ 이 엔드포인트만 **외부 네트워크(OpenReview API)를 탑니다.** papers 테이블에는
    최신 버전만 저장되기 때문입니다. 다른 조회보다 느리고 실패할 수 있으니, 프론트는
    사용자가 '수정 이력'을 명시적으로 눌렀을 때만 호출해야 합니다. 논문 상세와 함께
    미리 불러오지 마세요.

    ⚠️ supported=false면 **수정이 없었다는 뜻이 아니라 볼 수 없다는 뜻**입니다
    (2023년 이전 학회는 저자 수정 이력을 공개하지 않습니다). message에 사용자에게
    그대로 보여줄 수 있는 안내 문구가 들어 있으니, 빈 목록으로 처리하지 말고
    그 문구를 노출하세요.
    """
    revisions = get_paper_revisions(paper_id)
    if revisions is None:
        raise _NOT_FOUND
    return ApiResponse[RevisionsResponse](data=revisions)


@router.get("/{paper_id}/revisions/body-diff", response_model=ApiResponse[RevisionsResponse])
@limiter.limit(_BODY_DIFF_CALL_LIMIT)
def get_revisions_body_diff(
    request: Request,
    paper_id: int,
    refresh: bool = Query(
        default=False,
        description="캐시를 무시하고 다시 만듭니다 (훨씬 느리고, **로그인이 필요합니다**)"),
    user: User | None = Depends(get_current_user_optional),
):
    """`/revisions`에 pdf가 교체된 지점마다 본문 전체 diff를 얹은 버전.

    ⚠️ **`/revisions`보다 훨씬 느리고 비쌉니다.** pdf가 바뀐 지점마다 그 시점의
    PDF 두 개를 실제로 내려받아(중복은 제거) 텍스트를 뽑고 단어 단위로 비교합니다.
    LLM은 쓰지 않습니다 — 전부 결정론적 계산(PyMuPDF + difflib)이라 비용은 없지만
    OpenReview 왕복이 여러 번 붙어 첫 호출은 수 초가 걸릴 수 있습니다. 결과는
    캐시되므로 두 번째 호출부터는 빠릅니다.

    ⚠️ **IP 기준 시간당 10회로 제한됩니다** (`/revisions`의 30회보다 엄격 — 캐시
    미스 1건의 비용이 훨씬 크기 때문). 429가 오면 잠시 뒤 다시 시도하세요.

    ⚠️ `refresh=true`는 로그인이 필요합니다 (`/story`와 같은 이유 — 캐시를
    우회하므로 무한정 재생성시키는 걸 막습니다).

    응답 모양은 `/revisions`와 동일합니다(`FieldChange.field`가 자유 문자열이라
    스키마 변경 없이 `"body"` 값이 추가됩니다). pdf가 교체된 각 리비전의 `changes`
    배열에서 `field: "pdf"` 바로 다음에 `field: "body", kind: "text"` 항목이
    붙어 있고, `similarity`와 `segments`(단어 단위 추가/삭제)를 담고 있습니다.
    저장 공간을 아끼려고 `before`/`after`(전체 원문)는 담지 않습니다 — 필요하면
    `segments`에서 `op != "insert"`(수정 전), `op != "delete"`(수정 후)만 걸러
    이어 붙이면 재구성됩니다. 다운로드 실패·스캔본·페이지 상한 초과 등으로 일부
    transition은 `body` 항목이 아예 없을 수 있습니다 — 그건 실패가 아니라 그
    지점만 비교할 수 없었다는 뜻입니다.
    """
    if refresh and user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="캐시를 다시 만들려면 로그인이 필요합니다.",
            headers={"WWW-Authenticate": "Bearer"})

    revisions = get_paper_revisions_with_body(paper_id, refresh=refresh)
    if revisions is None:
        raise _NOT_FOUND
    return ApiResponse[RevisionsResponse](data=revisions)


@router.get("/{paper_id}/story", response_model=ApiResponse[StoryResponse])
@limiter.limit(_EXTERNAL_CALL_LIMIT)
def get_story(
    request: Request,
    paper_id: int,
    refresh: bool = Query(
        default=False,
        description="캐시를 무시하고 다시 만듭니다 (느리고, **로그인이 필요합니다**)"),
    user: User | None = Depends(get_current_user_optional),
):
    """이 논문이 무엇을 지적받아 무엇을 고쳤는지 — 재투고 궤적 + 심사 타임라인 + 요약.

    ⚠️ **비용이 드는 엔드포인트입니다.** 캐시에 없으면 OpenReview를 2번 타고, LLM이
    켜져 있으면 Sonnet까지 부릅니다. 그래서 두 가지 제한이 있습니다:

    - **IP 기준 시간당 30회.** 캐시된 논문을 다시 읽는 것은 비용이 없지만, 캐시에 없는
      논문을 연달아 훑는 것은 호출마다 돈이 나갑니다. 그걸 끊는 상한입니다.
    - **`refresh=true`는 로그인이 필요합니다.** 캐시를 우회하므로 같은 논문에 대고
      반복하면 상한을 우회해 무한히 재생성시킬 수 있습니다. 비로그인 요청은 이 값을
      무시하지 않고 **401로 거절합니다** — 조용히 캐시를 주면 호출자는 최신 결과를
      받았다고 오해합니다.

    유사 논문을 눌렀을 때 "이전엔 이랬는데 리뷰를 받고 이렇게 고쳤다"를 보여주는
    화면용입니다. `/{paper_id}`(원문·리뷰)와 `/{paper_id}/revisions`(수정 diff)를
    시간축으로 엮은 것이라, 이 엔드포인트를 쓰면 둘을 따로 부를 필요가 없습니다.

    응답은 세 부분이고 **서로 독립적으로 비어 있을 수 있습니다.**

    - `journey` — 재투고 궤적. DB만 쓰므로 항상 채워집니다. 재투고 기록이 없으면
      자기 자신 1개짜리(outcome='single')입니다.
    - `timeline` — 리뷰·저자 응답·수정본을 시간순으로 병합한 목록.
    - `narrative` — 요약. `used_llm=false`면 LLM이 꺼져 있어 결정론적 스텁입니다.

    ⚠️ **`timeline_supported=false`는 '심사가 없었다'가 아니라 '공개되지 않는다'는
    뜻입니다.** 빈 목록으로 처리하지 말고 `caveats`의 문구를 그대로 노출하세요.

    ⚠️ `timeline`의 리뷰 본문·점수는 **최종 수정본**입니다. 리뷰어가 저자 응답 이후
    리뷰를 고쳤으면 최초 게시 시점(`at`)의 내용과 다릅니다. 수정 전 점수는
    OpenReview가 공개하지 않으므로 **'몇 점에서 몇 점으로 올랐다'는 표시를 만들지
    마세요** — 수정이 있었다는 사실은 `review_update` 이벤트로 옵니다.

    ⚠️ 저자 수정으로 확인할 수 있는 범위는 제목·초록·첨부파일까지입니다. 본문 PDF
    내용은 알 수 없으니 "실험을 추가했다" 같은 표현을 화면에서 만들지 마세요.

    첫 호출은 OpenReview를 2번 타고(LLM을 켜면 Sonnet까지) 수 초가 걸립니다.
    결과를 캐시하므로 두 번째부터는 빠릅니다. 논문 상세와 함께 미리 부르지 말고
    사용자가 명시적으로 열었을 때 호출하세요.
    """
    if refresh and user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="캐시를 다시 만들려면 로그인이 필요합니다.",
            headers={"WWW-Authenticate": "Bearer"})

    story = get_paper_story(paper_id, refresh=refresh)
    if story is None:
        raise _NOT_FOUND
    return ApiResponse[StoryResponse](data=story)
