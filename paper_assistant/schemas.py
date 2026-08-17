"""공개 API 스키마 (백엔드 통합 계약).

백엔드 팀은 `analyze(...) -> Report` 하나와 이 Pydantic 모델들만 알면 된다.

**통계 레이어는 없다** — review_patterns(lift·Fisher 당락대조)·venue_trends·
rating_context·resubmission_flows를 걷어냈다. 이 서비스가 파는 것은 통계가 아니라
선정 5편의 리뷰 원문이고, 5편 위에서는 lift도 Fisher도 무의미하다. 되살리려면
docs/추천_파이프라인_재설계.md 결정 #1을 먼저 읽을 것.

논문별 유사도 **점수는 담지 않는다** (설계서 §20). 원시 코사인은 물론이고
백분위 변환도 못 쓴다 — 검색 top-20의 코사인 폭이 0.013이라 1위와 20위가 같은
값이 되기 때문이다. 대신 `rank`(순위), `match_type`(왜 걸렸는지),
그리고 쿼리 단위 `RetrievalConfidence`(결과를 믿어도 되는지)를 준다.
"""
from pydantic import BaseModel, Field, ValidationInfo, field_validator


class SimilarPaper(BaseModel):
    """검색 후보 1편. **화면에 보여줄 것이 아니다** — 그건 SelectedPaper다.

    후보 50편을 Report에 남기는 이유는 근거 추적이다. "검색이 무엇을 뽑았고 LLM이
    무엇을 골랐는가"의 관계가 이 파이프라인의 품질 지표라(재설계 문서 §4.4.1),
    후보를 버리면 나중에 잴 방법이 사라진다.
    """
    paper_id: int
    openreview_id: str
    title: str
    venue: str
    year: int
    decision: str
    rank: int = Field(description="검색 순위. 화면 순서가 아니다")
    match_type: str = Field(
        default="both",
        description="both(의미+용어 모두) | semantic(의미만) | lexical(용어만). "
                    "왜 이 논문이 후보에 걸렸는지의 근거.")


class ReviewDetail(BaseModel):
    """개별 리뷰 원문 (상세 보기 전용).

    2023년 이전 venue는 강/약점이 분리되지 않아 weaknesses에 본문 전체가 들어온다
    (init_db.sql의 needs_llm_split 주석 참고). 프론트는 이 경우를 '리뷰 본문'으로
    한 덩어리 표시해야 한다.

    **SelectedPaper보다 위에 있어야 한다.** SelectedPaper.reviews가 이 타입을
    참조하는데, Python 3.13까지는 어노테이션이 클래스 정의 시점에 평가되므로
    아래에 두면 import 자체가 NameError로 죽는다 (3.14는 PEP 649 지연 평가라
    가려진다 — 실제로 그렇게 가려져 있었다).
    """
    rating: float | None = None
    rating_raw: str | None = Field(
        default=None, description="'8: Accept' 등 원문. 척도가 연도마다 다름")
    confidence: float | None = None
    summary: str | None = None
    strengths: str | None = None
    weaknesses: str | None = None
    questions: str | None = None
    is_unsplit: bool = Field(
        default=False,
        description="참이면 강/약점 미분리 형식 — weaknesses가 리뷰 본문 전체")


class SelectedPaper(BaseModel):
    """**화면의 주인공** — LLM이 고른 논문 1편과 그 논문이 실제로 받은 리뷰.

    이 서비스가 하는 말은 "당신은 X를 지적받을 것입니다"가 아니라 "비슷한 논문들은
    이런 지적을 받았습니다"이고(README), 그 문장의 근거가 되는 단위가 이것이다.
    `Report.similar_papers`는 검색 후보 풀이고 **사용자에게 보여줄 것은 이쪽이다.**

    리뷰 전문을 Report에 함께 싣는 이유는 5편뿐이기 때문이다. 이웃 20편일 때는
    대부분 열람되지 않아 별도 조회로 뺐지만(PaperDetail 주석), 5편은 전부 보여주는
    것이 목적이라 화면마다 5번씩 더 부르는 편이 낭비다.
    """
    # --- 어떤 논문인가 ---
    paper_id: int
    openreview_id: str
    title: str
    venue: str
    year: int
    decision: str
    openreview_url: str = Field(description="리뷰 원본을 직접 확인할 수 있는 링크")
    pdf_url: str

    # --- 왜 골랐는가 (2단계 판정) ---
    rank: int = Field(description="표시 순서 (1이 가장 비슷하다고 판정된 논문)")
    reason: str = Field(description="왜 비슷한지 — 사용자에게 그대로 노출")
    confidence: str = Field(
        description="high | medium | low. **숫자 점수가 아닌 이유는 의도한 설계다** — "
                    "유사도 점수는 만들 수 없다(설계서 §20)")

    # --- 무슨 리뷰를 받았는가 (본론) ---
    meta_review: str | None = Field(default=None, description="AC 총평")
    reviews: list[ReviewDetail] = Field(
        default_factory=list,
        description="이 논문이 받은 리뷰 전문. **is_unsplit이 참인 리뷰는 강/약점이 "
                    "분리되지 않아 weaknesses에 본문 전체가 들어 있다** — '지적'으로 "
                    "표시하지 말고 '리뷰 본문'으로 한 덩어리 보여줄 것")
    avg_rating: float | None = None
    rating_count: int = 0
    rating_spread: float | None = Field(
        default=None, description="최고-최저 점수 차. 크면 리뷰어 의견이 갈렸다는 뜻")

    # --- 리뷰를 받고 얼마나 고쳤는가 ---
    revision_count: int | None = Field(
        default=None,
        description="저자가 **본문 PDF를 교체한 횟수**. 리비전 총 개수가 아니다 — "
                    "제목·초록만 고친 edit은 세지 않는다(query/revisions.py의 "
                    "count_body_revisions). **None과 0은 다른 값이다**: None은 "
                    "'볼 수 없다'(2023년 이전 학회는 수정 이력이 비공개, 조회 실패 "
                    "포함), 0은 '확인해 보니 본문을 안 고쳤다'. 화면이 이 둘을 "
                    "'—'와 '없음'으로 다르게 그리므로 0으로 뭉개면 안 된다")


class PaperSelection(BaseModel):
    """LLM이 후보 중에서 고른 논문 1편과 그 이유 (2단계 재정렬의 원출력).

    파이프라인 내부용이다 — review_fetch_node가 여기에 논문 메타와 리뷰를 붙여
    SelectedPaper로 만들고, Report에 실리는 것은 그쪽이다.

    1단계 검색은 제목·초록 임베딩만 보지만, 여기서는 입력 논문의 **PDF 원본**과
    후보 목록을 함께 넘겨 본문·실험·참고문헌까지 읽고 고르게 한다. 검색 상위 후보
    50편은 전부 코사인 0.93+ 대역이라 임베딩으로는 그 안에서 순위를 매길 수 없고
    (설계서 §20), 그 구간을 실제로 가를 수 있는 것이 이 단계다.

    ⚠️ `paper_id`는 **후보 목록과 대조해 검증된 값**이다. 모델이 지어낸 id는
    graph/nodes.py에서 제거되므로, 여기 남은 것은 전부 실재하는 논문을 가리킨다.
    """
    paper_id: int
    rank: int = Field(description="표시 순서 (1이 가장 비슷하다고 판정된 논문)")
    reason: str = Field(description="왜 비슷하다고 판단했는지 — 사용자에게 그대로 노출")
    confidence: str = Field(
        description="high | medium | low. **숫자 점수가 아닌 이유는 의도한 설계다** — "
                    "유사도 점수는 만들 수 없다(설계서 §20)")


class ReviewPointDetail(BaseModel):
    """이 논문의 리뷰에서 추출된 개별 지적 항목.

    ⚠️ 추출기는 리뷰의 weaknesses 필드를 통째로 weakness로 라벨링한다
    (review_extractor.HeuristicExtractor). 강/약점 미분리 포맷 리뷰는 이 필드가
    본문 전체라, 거기서 나온 항목은 실제 지적이 아닐 수 있다 —
    from_unsplit_review로 구분해 '지적'이라 단정하지 않는다.
    """
    aspect: str
    sentiment: str = Field(description="weakness | strength | question")
    text: str
    from_unsplit_review: bool = Field(
        default=False,
        description="강/약점 미분리 리뷰에서 나온 항목. 참이면 지적으로 단정 금지")


class PaperDetail(BaseModel):
    """유사 논문 1편의 상세 (get_paper_detail 반환).

    Report에 싣지 않고 별도로 조회한다 — 이웃 20편의 리뷰 전문을 매 분석마다
    실어 보내면 대부분 열람되지 않는 데이터이고, analyze() 계약도 무거워진다.
    원문 PDF는 저장하지 않으므로 OpenReview/arXiv 링크만 제공한다.
    """
    paper_id: int
    openreview_id: str
    title: str
    abstract: str | None = None
    authors: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    primary_area: str | None = None
    venue: str
    year: int
    decision: str
    meta_review: str | None = None

    openreview_url: str = Field(description="OpenReview 포럼(리뷰 원본) 링크")
    pdf_url: str = Field(description="OpenReview PDF 링크")
    arxiv_url: str | None = Field(default=None, description="arXiv 링크 (있을 때만)")

    reviews: list[ReviewDetail] = Field(default_factory=list)
    review_points: list[ReviewPointDetail] = Field(default_factory=list)


class PaperSummary(BaseModel):
    """논문 목록 조회(list_papers) 1건. 상세는 get_paper_detail로 별도 조회한다."""
    paper_id: int
    openreview_id: str
    title: str
    venue: str
    year: int
    decision: str
    primary_area: str | None = None


class PaperListResponse(BaseModel):
    """list_papers() 반환. total은 필터 적용 후 전체 건수(페이지네이션용)."""
    total: int
    items: list[PaperSummary] = Field(default_factory=list)


class DiffSegment(BaseModel):
    """텍스트 변경의 단어 단위 조각. 프론트가 색만 입혀 그리면 된다."""
    op: str = Field(description="equal | insert | delete | moved")
    text: str


class FieldChange(BaseModel):
    """리비전 1건에서 바뀐 필드 하나."""
    field: str = Field(description="OpenReview content 필드명")
    label: str = Field(description="화면 표시용 한국어 라벨")
    kind: str = Field(
        description="text(단어 diff) | file(교체 여부만) | value | image(그림·표 전후 비교)")
    before: str | None = None
    after: str | None = None
    similarity: float | None = Field(
        default=None,
        description="text일 때 단어 단위 유사도(1.0=동일)")
    segments: list[DiffSegment] = Field(
        default_factory=list, description="text일 때만. 길면 잘려 있을 수 있다")

    # 파일(pdf/보충자료)일 때만. 그 시점 파일을 실제로 내려받는 링크다 —
    # content에 실린 /pdf/<해시>.pdf 경로는 교체되면 404라 쓸 수 없다(실측).
    before_url: str | None = None
    after_url: str | None = None

    # kind == "image"일 때만 — 그림·표 둘 다 하이라이트 없이 전/후를 나란히
    # 보여준다. 표도 텍스트로 재구성하지 않고 이미지로 비교한다 — find_tables()가
    # 병합된 셀을 못 풀어내 원본과 많이 달라지는 것보다, 영역을 그대로 잘라
    # 보여주는 쪽이 원본에 더 가깝다고 판단했다(paper_assistant/pdf/extract.py의
    # extract_media_images). 픽셀 단위 비교는 안 한다(오탐이 잦고 범위 밖).
    before_image: str | None = Field(
        default=None, description="data:image/png;base64,... 한쪽에만 있으면 추가/삭제된 것")
    after_image: str | None = None

    # kind == "image"이고 저자가 번호를 재배치했을 때만(예: v2의 Figure 6가
    # v3에서 Figure 5가 됨). label은 항상 "수정 전(before) 번호" 기준이고,
    # after_label은 "수정 후(after) 문서에서 실제로 불리는 번호"다. 두 값이
    # 다르면 프론트는 v2 본문에 나오는 "(그림 6)"은 label로, v3 본문에 나오는
    # "(그림 5)"는 after_label로 각각 찾아야 한다 — 하나의 평평한 라벨→이미지
    # 표로 합치면, 서로 다른 두 그림이 번호를 주고받아 우연히 같은 번호를
    # 쓰게 될 때(실측: v2 Figure 7이 v3 Figure 6가 되면서, v2 Figure 6→v3
    # Figure 5인 별개의 그림과 "Figure 6"라는 번호가 겹침) 한쪽이 다른 쪽을
    # 덮어써 버린다.
    after_label: str | None = Field(
        default=None, description="번호가 재배치됐을 때 수정 후 문서에서의 라벨. 같으면 None")


class RevisionEntry(BaseModel):
    """저자가 제출본을 고친 시점 1건."""
    revision_id: str
    kind: str = Field(
        description="submission | rebuttal | camera_ready | withdrawal | other")
    kind_label: str
    timestamp: int = Field(description="epoch ms")
    date: str = Field(description="YYYY-MM-DD HH:MM (KST)")
    changes: list[FieldChange] = Field(default_factory=list)
    is_baseline: bool = Field(
        default=False,
        description="관측 가능한 첫 버전. 이전 내용을 모르므로 diff가 없다")


class PaperRevisions(BaseModel):
    """논문 1편의 수정 이력 (get_paper_revisions 반환).

    ⚠️ papers 테이블에는 **최신 버전만** 저장된다 — openreview_id를 자연 키로
    upsert하므로 수정 전 내용은 남지 않는다(db/load.py). 그래서 이 조회는 DB가
    아니라 OpenReview API를 실시간으로 때린다. 느리고(수백 ms) 실패할 수 있어
    analyze()나 PaperDetail에 합치지 않고 별도 호출로 뺐다.

    2023년 이전 venue(v1 API)는 저자 수정 이력이 공개되지 않는다 — 읽을 수 있는
    건 code 링크·게재일 같은 운영 메타데이터뿐이라 supported=False로 돌려준다.
    없는 걸 빈 목록으로 주면 '수정이 없었다'로 오독되기 때문이다.
    """
    paper_id: int
    openreview_id: str
    supported: bool = Field(description="이 venue에서 수정 이력을 볼 수 있는가")
    message: str | None = Field(
        default=None, description="불가 사유 또는 '이력 없음' 안내. 그대로 노출 가능")
    revisions: list[RevisionEntry] = Field(default_factory=list)
    cached_at: str | None = Field(
        default=None,
        description="캐시에서 나온 응답이면 최초 생성 시각(ISO). 방금 만든 것이면 None")


class JourneyStop(BaseModel):
    """재투고 궤적의 한 지점 — 같은 논문이 어느 학회에 내서 어떻게 됐는지."""
    paper_id: int
    openreview_id: str
    title: str
    venue: str
    year: int
    decision: str
    avg_rating: float | None = None
    rating_count: int = 0
    rating_vs_venue: float | None = Field(
        default=None,
        description="같은 venue 평균 대비(+면 평균 이상). 원점수는 척도가 venue마다 "
                    "달라 **stop끼리 직접 비교하면 안 된다** — 이 값으로 비교할 것")
    is_query: bool = Field(
        default=False, description="사용자가 조회한 그 논문인가")
    match_method: str | None = Field(
        default=None,
        description="직전 stop과 이어 붙인 근거. arxiv_id | title_exact | "
                    "title_author_fuzzy. 첫 stop은 None")
    match_confidence: float | None = None


class SubmissionJourney(BaseModel):
    """같은 논문의 투고 궤적 (get_paper_story의 1부).

    submission_links를 **양방향으로 전이 순회**해서 만든다 — 링크는 인접 쌍만
    기록하므로(ICLR23→ICLR24, ICLR24→ICLR25) 한 방향만 따라가면 궤적의 절반이
    잘린다.

    ⚠️ 링크는 제목 일치로 추정한 것이라 확실한 사실이 아니다. title_author_fuzzy는
    특히 동명 논문을 잘못 묶을 수 있어 match_confidence를 함께 노출한다.
    """
    stops: list[JourneyStop] = Field(
        default_factory=list, description="연도 오름차순. 단일 투고면 1개")
    outcome: str = Field(
        default="single",
        description="single(재투고 기록 없음) | improved(탈락 뒤 통과) | "
                    "still_rejected(재투고했지만 또 탈락) | mixed(그 외)")
    message: str | None = Field(
        default=None, description="사용자에게 그대로 보여줄 한 줄 요약")


class TimelineEvent(BaseModel):
    """심사 과정의 사건 1건. 리뷰·저자 응답·수정본을 한 시간축에 올린 단위.

    kind별로 채워지는 필드가 다르다:
      review          → review (점수·본문)
      review_update   → rating (최종 점수). 아래 경고 참고
      rebuttal        → text (저자가 쓴 응답 원문)
      comment         → text (리뷰어·AC가 쓴 후속 코멘트)
      author_revision → changes (기존 FieldChange 그대로), is_baseline
      meta_review     → text
      decision        → text

    ⚠️ **review 이벤트의 본문·점수는 '최종 수정본'이다.** 리뷰어가 저자 응답을
    보고 리뷰를 고쳤어도 우리가 가진 건 고쳐진 뒤의 내용뿐이라, 그것을 최초 게시
    시각(at)에 붙여 보여주게 된다. 수정 전 내용은 복원할 방법이 없다 — OpenReview
    edit 이력에서 리뷰의 첫 edit은 content가 빈 채로 내려온다(실측 3건 전부).
    그래서 수정이 있었던 리뷰는 review_update 이벤트를 따로 세워 "이 시점에
    고쳐졌고, 고치기 전 점수는 공개되지 않는다"를 명시한다.
    """
    event_id: str
    at: int = Field(description="epoch ms. 정렬 키")
    date: str = Field(description="YYYY-MM-DD HH:MM (KST)")
    kind: str = Field(
        description="review | review_update | rebuttal | comment | "
                    "author_revision | meta_review | decision")
    kind_label: str = Field(description="화면 표시용 한국어 라벨")
    actor: str = Field(description="'리뷰어 AHeX' | '저자' | 'AC' 등 표시용")
    headline: str = Field(description="한 줄 요약. 접힌 상태에서 보여줄 문장")

    review: ReviewDetail | None = None
    rating: float | None = Field(
        default=None,
        description="review_update일 때 **최종** 점수. 수정 전 점수는 구할 수 없어 "
                    "'몇 점에서 몇 점으로 올랐다'는 표현을 만들면 안 된다")
    text: str | None = Field(
        default=None, description="rebuttal·meta_review·decision 원문")
    changes: list[FieldChange] = Field(default_factory=list)
    is_baseline: bool = Field(
        default=False, description="author_revision 중 관측 가능한 첫 버전")


class StoryNarrative(BaseModel):
    """'무엇을 지적받아 무엇을 고쳤는가' 요약 (get_paper_story의 3부).

    ⚠️ **근거의 한계가 구조적이다.** 우리가 볼 수 있는 수정은 제목·초록·첨부파일
    교체 여부뿐이고, 실제 논문 본문이 어떻게 바뀌었는지는 PDF 안에 있어 알 수
    없다. 그래서 여기 문장은 "리뷰어가 X를 지적했고 저자가 Y라고 답했으며,
    초록에는 Z가 반영됐다"까지만 말한다 — 본문 반영 여부는 단정하지 않는다.
    evidence_scope가 그 한계를 명시하므로 프론트는 이 값을 함께 노출할 것.
    """
    headline: str = Field(description="한 문장 요약")
    reviewers_asked: list[str] = Field(
        default_factory=list, description="리뷰어가 요구한 것")
    authors_changed: list[str] = Field(
        default_factory=list, description="저자가 응답하거나 실제로 고친 것")
    outcome_note: str = Field(
        default="", description="그래서 결과가 어떻게 됐는지")
    evidence_scope: str = Field(
        description="abstract_only(제목·초록 diff만 확인) | replies_only(수정 이력 "
                    "비공개 venue라 리뷰·응답만 확인) — 근거의 한계를 그대로 노출할 것")
    used_llm: bool = Field(
        default=False,
        description="실제 LLM 호출로 만들어졌는지. False면 결정론적 스텁이다 "
                    "(Report.used_llm과 같은 규약 — 설정값이 아니라 실행 결과)")


class PaperStory(BaseModel):
    """논문 1편의 심사 서사 (get_paper_story 반환).

    "이 논문은 처음엔 이랬는데, 이런 지적을 받고, 이렇게 고쳤다"를 한 응답으로
    답한다. 세 부분이 서로 독립적으로 실패할 수 있게 설계돼 있다 — journey는 DB만
    쓰므로 항상 채워지고, timeline은 외부 API가 죽으면 비고, narrative는 LLM이
    꺼져 있으면 스텁이다. 한 부분이 비어도 나머지는 보여줄 것.
    """
    paper_id: int
    openreview_id: str
    title: str
    venue: str
    year: int
    decision: str

    journey: SubmissionJourney = Field(default_factory=lambda: SubmissionJourney())
    timeline: list[TimelineEvent] = Field(default_factory=list)
    timeline_supported: bool = Field(
        default=False,
        description="이 venue에서 심사 타임라인을 볼 수 있는가. False는 '심사가 "
                    "없었다'가 아니라 **공개되지 않는다**는 뜻이다")
    narrative: StoryNarrative | None = None
    caveats: list[str] = Field(
        default_factory=list,
        description="이 응답으로 알 수 없는 것. 사용자에게 그대로 노출 가능한 문장")
    cached_at: str | None = Field(
        default=None,
        description="캐시에서 나온 응답이면 최초 생성 시각(ISO). 방금 만든 것이면 None")


# ─────────────────────────────────────────────────────────────────────────
# 아래 둘은 **분석 파이프라인에서 쓰이지 않는다.** 통계 레이어를 걷어내면서
# analyze() 경로에서 빠졌지만, graph/clustering.py 의 aspect 집계가 이 모양을
# 반환하고 scripts/eval_retrieval.py 가 그 집계로 §24 평가를 재현한다. 통계
# 레이어를 되살릴 때의 출발점이기도 하다 (재설계 문서 결정 #1).
# ─────────────────────────────────────────────────────────────────────────

class ReviewExample(BaseModel):
    """패턴을 대표하는 실제 리뷰 지적 문장 1건.

    문자열이 아니라 출처를 함께 담는다 — 요약문의 인용 표기가 이 문장으로
    역추적되어야 하기 때문이다 (graph/evidence.py).
    """
    text: str
    paper_id: int = Field(description="이 지적을 받은 코퍼스 논문 id")
    review_point_id: int | None = Field(
        default=None, description="review_points.id. 근거 추적의 최소 단위")
    from_unsplit_review: bool = Field(
        default=False,
        description="강/약점 미분리 리뷰에서 나온 문장. 참이면 '지적'이라 단정 금지 "
                    "(대표 문장 선정에서 이미 뒤로 밀리지만, 다른 후보가 없으면 뽑힌다)")


class EvidenceItem(BaseModel):
    """요약이 인용할 수 있는 근거 1건 (검색된 원문).

    `Report.summary_markdown`의 `[E1]` / `[M1]` 표기가 여기 label을 가리킨다.
    풀에 없는 라벨은 생성 직후 제거되므로, 화면에 남은 인용은 전부 실제
    리뷰·메타리뷰 원문으로 **역추적된다**.

    ⚠️ 역추적이 가능하다는 것이지, 그 원문이 인용한 문장을 **뒷받침한다**는
    보장은 아니다. 검증은 라벨의 실재만 확인한다 (graph/evidence.py 참고).
    """
    label: str = Field(description="E1, M1 … 요약문에서 참조하는 키")
    kind: str = Field(description="review_point(리뷰 지적) | meta_review(AC 총평)")
    text: str
    paper_id: int
    paper_title: str = ""
    review_point_id: int | None = None
    aspect: str | None = Field(default=None, description="review_point일 때")
    decision: str | None = Field(default=None, description="meta_review일 때")
    from_unsplit_review: bool = Field(
        default=False, description="미분리 리뷰 출처 — '지적'이라 단정 금지")


class ReviewPattern(BaseModel):
    """유사 논문들에서 반복 등장하는 지적 패턴.

    단순 빈도만으로는 정보가 없다 — baselines 지적은 코퍼스 전체 78.8%의 논문이
    받으므로 "20편 중 17편"은 사실상 상수다(설계서 §18). 그래서 코퍼스 base rate
    대비 **lift**와 이항검정 p값을 함께 싣고, 당락 대조까지 붙인다.
    """
    label: str = Field(description="aspect 표시 라벨")
    aspect: str = Field(description="통제된 분류 또는 'other'")
    paper_count: int = Field(description="이 지적을 받은 유사 논문 수")
    total_papers: int = Field(description="분석 대상 유사 논문 총수")
    examples: list[ReviewExample] = Field(
        default_factory=list,
        description="서로 다른 논문에서 뽑은 대표 지적 문장 (출처 id 포함)")

    # --- base rate 대비 두드러짐 (base_rates 미제공 시 None) ---
    base_rate: float | None = Field(
        default=None, description="코퍼스 전체에서 이 지적을 받는 논문 비율(0~1)")
    lift: float | None = Field(
        default=None, description="이웃 지적률 / base_rate. 1이면 평범, >1이면 이 주제 특유")
    p_value: float | None = Field(
        default=None, description="이항검정 단측 p값 (관측 방향 기준)")
    is_distinctive: bool = Field(
        default=False, description="이 주제군에서 유의하게 두드러진 지적인지")

    # --- 당락 대조: 이 지적을 받은 이웃 vs 받지 않은 이웃 ---
    accept_with: int = Field(default=0, description="이 지적을 받은 이웃 중 accept 수")
    accept_without: int = Field(default=0, description="이 지적이 없는 이웃 중 accept 수")
    decided_with: int = Field(default=0, description="이 지적을 받은 이웃 중 결과 확정 수")
    decided_without: int = Field(default=0, description="이 지적이 없는 이웃 중 결과 확정 수")
    accept_rate_with: float | None = Field(
        default=None, description="이 지적을 받고도 통과한 비율(0~1)")
    accept_rate_without: float | None = Field(
        default=None, description="이 지적이 없을 때 통과 비율(0~1)")
    contrast_p_value: float | None = Field(
        default=None, description="당락 대조의 단측 Fisher 정확검정 p값")
    is_contrast_significant: bool = Field(
        default=False,
        description="당락 격차가 통계적으로 유의한지. False면 표본 부족 — 단정 금지")


class RetrievalConfidence(BaseModel):
    """검색 결과 전체를 믿어도 되는지 (설계서 §20).

    논문 사이는 못 가르지만 쿼리 사이는 잘 갈린다 — 도메인 안 쿼리의 top-5 평균
    코사인은 0.946~0.966, 도메인 밖은 0.852~0.867로 겹치지 않는다.
    이 판정이 없으면 "치즈 숙성 미생물학"에도 ML 논문 20편을 자신있게 내놓는다.
    """
    level: str = Field(default="strong", description="strong | moderate | weak")
    message: str = Field(default="", description="사용자에게 보여줄 설명")
    is_reliable: bool = Field(
        default=True, description="False면 프론트는 결과를 경고와 함께 표시할 것")
    evidence: float = Field(
        default=0.0,
        description="top-5 평균 코사인. **진단용 — 사용자에게 노출 금지**")


class ProgressEvent(BaseModel):
    """분석이 지금 어디까지 왔는지 1건 (analyze(on_event=...)로 흘러나온다).

    **사용자 화면에 그대로 띄울 수 있는 단위다.** 분석은 수십 초~수 분이 걸리는데
    그동안 백엔드가 아는 것이 pending/running뿐이라 화면도 "분석 중…" 한 줄밖에
    쓸 수 없었다. 그 침묵을 없애려고 파이프라인이 단계마다 이걸 내보낸다.

    `label`은 **한국어 완성 문장**이다. 화면이 step id로 문구를 만들지 않는 이유는,
    같은 단계라도 실제로 한 일이 다르기 때문이다 — 재정렬은 LLM이 고른 경우와
    검색 상위를 그대로 쓴 경우가 있고, 그 둘을 같은 문구로 덮으면 스텁 결과가
    LLM 판정으로 오인된다(Report.used_llm과 같은 규약). 무엇을 했는지 아는 쪽이
    문구를 만든다.

    ⚠️ **진행률(%)은 담지 않는다.** 단계별 소요 시간이 극단적으로 불균등해서
    (첫 호출 모델 로드 수십 초, 재정렬 수십 초, 나머지는 수 초) 어떤 비율도
    사실이 아니게 된다. 유사도 점수를 만들지 않는 것과 같은 이유다(설계서 §20).
    """
    step: str = Field(
        description="prepare | extract | retrieval | rerank | review_fetch | synthesis. "
                    "화면이 단계 순서와 아이콘을 정하는 키")
    done: bool = Field(
        default=False,
        description="False면 이 단계를 시작했다(진행 중), True면 끝났다. 같은 step으로 "
                    "두 번 온다 — 화면은 step 기준으로 최신 것을 쓰면 된다")
    label: str = Field(description="사용자에게 그대로 보여줄 한 줄")
    detail: str | None = Field(
        default=None,
        description="곁들일 한 줄 (선정 논문 제목, 신뢰도 경고 등). 없을 수 있다")
    at: str = Field(description="발생 시각 (ISO 8601, UTC)")


SIMILARITY_FOCUS_VALUES = ("problem", "method", "evaluation", "balanced")
RECENCY_BIAS_VALUES = ("recent", "cited", "balanced")

# 모르는 값이 왔을 때 떨어질 자리. "값이 없다 = 균형있게"는 온보딩 저장 규약과
# 같다 (app/models/onboarding.py — nullable에 sentinel을 두지 않는 이유).
_DEFAULT_PREFERENCE = "balanced"

_ALLOWED_PREFERENCES = {
    "similarity_focus": SIMILARITY_FOCUS_VALUES,
    "recency_bias": RECENCY_BIAS_VALUES,
}


class SearchPreferences(BaseModel):
    """온보딩 2단계 답변 — "유사 논문을 고를 때 무엇을 우선할까".

    similarity_focus는 **2단계 LLM 재정렬의 판정 기준**을 바꾸고(어떤 축의 일치를
    더 무겁게 볼지), recency_bias는 **1단계 검색의 랭킹 가중치**를 바꾼다
    (hybrid_search.RANKING_PRESETS). 두 값이 서로 다른 층에 걸리는 것은 우연이
    아니다 — 임베딩은 문제/방법/평가를 구분하지 못하고(제목+초록을 벡터 하나로
    만든다), 반대로 LLM에게는 연도·인용 백분위를 판정 근거로 줄 수 없다.

    ⚠️ **화이트리스트 밖의 값은 조용히 balanced로 떨어진다.** 이 값의 출처가
    `POST /api/onboarding`인데 그 엔드포인트는 **인증이 없고**(회원가입 전에
    불린다) 컬럼은 자유 문자열 String(50)이다. 즉 누구든 임의의 문장을 넣을 수
    있고, 그 문자열이 그대로 시스템 프롬프트에 붙으면 프롬프트 인젝션 경로가
    된다. 그래서 문자열을 프롬프트로 나르지 않고 **여기서 걸러낸 키로 상수
    테이블을 조회**한다 (graph/nodes.py의 _FOCUS_RULES, hybrid_search의
    RANKING_PRESETS). 422로 거절하지 않고 떨어뜨리는 이유는, 이것이 사용자 입력
    검증이 아니라 이미 저장된 값을 읽는 자리이기 때문이다 — 옛 값이나 프론트가
    선택지를 늘렸다 되돌린 흔적 때문에 분석이 통째로 실패하면 안 된다.
    """
    similarity_focus: str = Field(
        default=_DEFAULT_PREFERENCE,
        description="problem | method | evaluation | balanced. 2단계 재정렬이 "
                    "어떤 축의 일치를 더 무겁게 볼지")
    recency_bias: str = Field(
        default=_DEFAULT_PREFERENCE,
        description="recent | cited | balanced. 1단계 랭킹 가중치 프리셋을 고른다")

    @field_validator("similarity_focus", "recency_bias", mode="before")
    @classmethod
    def _known_value_or_balanced(cls, v, info: ValidationInfo):
        """None·오타·주입 시도를 전부 balanced로 접는다 (위 ⚠️ 참고)."""
        allowed = _ALLOWED_PREFERENCES[info.field_name]
        return v if v in allowed else _DEFAULT_PREFERENCE

    @property
    def is_default(self) -> bool:
        """둘 다 기본값인가 — 화면이 "맞춤 적용" 문구를 띄울지 정하는 데 쓴다."""
        return (self.similarity_focus == _DEFAULT_PREFERENCE
                and self.recency_bias == _DEFAULT_PREFERENCE)


class Report(BaseModel):
    """analyze()의 최종 반환. 프론트가 섹션별로 렌더링할 수 있도록 구조화."""
    query_title: str
    query_abstract: str
    confidence: RetrievalConfidence = Field(
        default_factory=lambda: RetrievalConfidence())
    similar_papers: list[SimilarPaper] = Field(default_factory=list)

    # --- 2단계 LLM 재정렬 결과 (화면의 주인공) ---
    selected_papers: list[SelectedPaper] = Field(
        default_factory=list,
        description="LLM이 고른 논문과 그 논문이 받은 리뷰 (최대 5편). **화면에 보여줄 "
                    "것은 이것이고 similar_papers는 후보 풀이다.** 비어 있을 수 있다 — "
                    "검색 신뢰도가 weak이면 재정렬을 돌리지 않고, 정말 비슷한 논문이 "
                    "5편이 안 되면 모델이 더 적게 고른다(빈 목록도 정직한 답이다). "
                    "비었을 때 후보 풀을 대신 보여주면 안 된다 — 그게 이 단계를 넣은 "
                    "이유를 무효로 만든다.")


    # --- 근거 추적 (RAG) ---
    evidence: list[EvidenceItem] = Field(
        default_factory=list,
        description="요약이 인용할 수 있는 검색된 원문 풀. LLM을 끄더라도 채워지므로 "
                    "프론트는 항상 '이 결론의 출처' 목록을 보여줄 수 있다.")
    citations: list[str] = Field(
        default_factory=list,
        description="summary_markdown이 실제로 인용한 라벨. 풀에 없는 라벨은 "
                    "생성 직후 제거되므로 여기 남은 것은 전부 유효하다.")

    summary_markdown: str = Field(
        default="",
        description="사람이 읽는 종합 요약. [E1]/[M1] 표기는 evidence의 label을 가리킨다")
    used_llm: bool = Field(
        default=False,
        description="이 리포트가 실제 LLM 호출로 만들어졌는지. False면 태깅·요약이 "
                    "결정론적 스텁이다. 근거 추적용 — 설정값이 아니라 실행 결과다.")
    preferences: SearchPreferences = Field(
        default_factory=SearchPreferences,
        description="이 분석에 **실제로 적용된** 온보딩 선호. used_llm과 같은 규약이다 "
                    "— 온보딩 테이블의 현재 값이 아니라 이 실행이 쓴 값이라, 사용자가 "
                    "나중에 마이페이지에서 답을 바꿔도 지난 분석의 기록은 변하지 않는다. "
                    "이게 없으면 '왜 이 5편이 나왔나'를 사후에 가릴 수 없고, "
                    "similar_paper_matches에 쌓이는 품질 신호도 프리셋별로 나눌 수 없다.")
