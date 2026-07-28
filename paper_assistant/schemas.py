"""공개 API 스키마 (백엔드 통합 계약).

백엔드 팀은 `analyze(...) -> Report` 하나와 이 Pydantic 모델들만 알면 된다.

논문별 유사도 **점수는 담지 않는다** (설계서 §20). 원시 코사인은 물론이고
백분위 변환도 못 쓴다 — 검색 top-20의 코사인 폭이 0.013이라 1위와 20위가 같은
값이 되기 때문이다. 대신 `rank`(순위), `match_type`(왜 걸렸는지),
그리고 쿼리 단위 `RetrievalConfidence`(결과를 믿어도 되는지)를 준다.
"""
from pydantic import BaseModel, Field


class SimilarityTag(BaseModel):
    """왜 유사한가에 대한 구조화된 근거 (설계서 4.1)."""
    kind: str = Field(description="methodology | dataset | problem_setting | citation")
    reason: str = Field(description="한 줄 근거")


class SimilarPaper(BaseModel):
    paper_id: int
    openreview_id: str
    title: str
    venue: str
    year: int
    decision: str
    rank: int
    match_type: str = Field(
        default="both",
        description="both(의미+용어 모두) | semantic(의미만) | lexical(용어만). "
                    "왜 이 논문이 걸렸는지의 근거.")
    tags: list[SimilarityTag] = Field(default_factory=list)

    # --- 리뷰 점수 (§19). 원점수는 venue별 척도가 달라 단독 해석 금지 ---
    avg_rating: float | None = Field(
        default=None, description="이 논문이 받은 리뷰 점수 평균")
    rating_count: int = Field(default=0, description="리뷰 수")
    rating_spread: float | None = Field(
        default=None, description="최고-최저 점수 차. 크면 리뷰어 의견이 갈렸다는 뜻")
    rating_vs_venue: float | None = Field(
        default=None, description="같은 venue 평균 대비 차이(+면 평균 이상)")
    rating_vs_threshold: float | None = Field(
        default=None, description="당락 경계 대비 차이. 편향 venue는 None")


class ReviewDetail(BaseModel):
    """개별 리뷰 원문 (상세 보기 전용).

    2023년 이전 venue는 강/약점이 분리되지 않아 weaknesses에 본문 전체가 들어온다
    (init_db.sql의 needs_llm_split 주석 참고). 프론트는 이 경우를 '리뷰 본문'으로
    한 덩어리 표시해야 한다.
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


class DiffSegment(BaseModel):
    """텍스트 변경의 단어 단위 조각. 프론트가 색만 입혀 그리면 된다."""
    op: str = Field(description="equal | insert | delete")
    text: str


class FieldChange(BaseModel):
    """리비전 1건에서 바뀐 필드 하나."""
    field: str = Field(description="OpenReview content 필드명")
    label: str = Field(description="화면 표시용 한국어 라벨")
    kind: str = Field(description="text(단어 diff 있음) | file(교체 여부만) | value")
    before: str | None = None
    after: str | None = None
    similarity: float | None = Field(
        default=None, description="text일 때 단어 단위 유사도(1.0=동일)")
    segments: list[DiffSegment] = Field(
        default_factory=list, description="text일 때만. 길면 잘려 있을 수 있다")

    # 파일(pdf/보충자료)일 때만. 그 시점 파일을 실제로 내려받는 링크다 —
    # content에 실린 /pdf/<해시>.pdf 경로는 교체되면 404라 쓸 수 없다(실측).
    before_url: str | None = None
    after_url: str | None = None


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
    examples: list[str] = Field(default_factory=list, description="지적 문장 예시")

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


class VenueTrend(BaseModel):
    """유사 논문들의 게재 학회/결과 경향.

    ⚠️ accept_rate를 실제 채택률로 해석하면 안 되는 venue가 있다 — NeurIPS는
    코퍼스의 95%가 accept다(OpenReview가 채택 논문 위주로만 공개). `is_coverage_biased`
    가 참이면 절대값이 아니라 코퍼스 대비 상대값(lift)으로만 말한다 (설계서 §19).
    """
    venue: str
    year: int | None = None
    paper_count: int
    accept_count: int
    accept_rate: float
    corpus_accept_rate: float | None = Field(
        default=None, description="이 학회 코퍼스 전체의 accept 비율 (비교 기준선)")
    accept_lift: float | None = Field(
        default=None, description="이웃 accept율 / 코퍼스 accept율")
    is_coverage_biased: bool = Field(
        default=False, description="참이면 accept율 절대값 해석 금지 — 표본이 채택 편향")


class RatingContext(BaseModel):
    """이웃 논문들의 리뷰 점수 분포와 당락 기준선 (§19).

    "비슷한 논문들은 몇 점을 받았고, 붙으려면 몇 점이 필요한가"에 답한다.
    """
    neighbor_mean: float | None = Field(default=None, description="이웃 평균 점수")
    accepted_mean: float | None = Field(default=None, description="통과한 이웃의 평균")
    rejected_mean: float | None = Field(default=None, description="탈락한 이웃의 평균")
    rated_papers: int = Field(default=0, description="점수가 있는 이웃 수")
    threshold: float | None = Field(
        default=None, description="당락 경계 추정치 (편향 없는 venue 기준)")
    threshold_venue: str | None = Field(
        default=None, description="경계를 가져온 venue")
    split_papers: list[str] = Field(
        default_factory=list,
        description="리뷰어 의견이 크게 갈린 논문 제목 (spread 상위)")
    biased_venues: list[str] = Field(
        default_factory=list,
        description="표본이 채택 편향된 venue 목록 — accept율 절대해석 금지")


class ResubmissionFlow(BaseModel):
    """재투고 흐름 (예: ICLR reject → NeurIPS accept)."""
    from_venue: str
    to_venue: str
    count: int


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


class Report(BaseModel):
    """analyze()의 최종 반환. 프론트가 섹션별로 렌더링할 수 있도록 구조화."""
    query_title: str
    query_abstract: str
    confidence: RetrievalConfidence = Field(
        default_factory=lambda: RetrievalConfidence())
    similar_papers: list[SimilarPaper] = Field(default_factory=list)
    review_patterns: list[ReviewPattern] = Field(default_factory=list)
    venue_trends: list[VenueTrend] = Field(default_factory=list)
    rating_context: RatingContext = Field(default_factory=lambda: RatingContext())
    resubmission_flows: list[ResubmissionFlow] = Field(default_factory=list)
    summary_markdown: str = Field(
        default="", description="사람이 읽는 종합 요약")
    used_llm: bool = Field(
        default=False,
        description="이 리포트가 실제 LLM 호출로 만들어졌는지. False면 태깅·요약이 "
                    "결정론적 스텁이다. 근거 추적용 — 설정값이 아니라 실행 결과다.")
