-- 논문 리서치 어시스턴트 스키마 (Postgres + pgvector)
-- docker compose up 시 최초 1회 자동 실행된다.
-- 설계: AI_파트_설계서.md §3, 실측 결과 §10~11 반영.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- 재투고 매칭의 제목 유사도용

-- ---------------------------------------------------------------- papers
-- 검색의 기본 단위. 논문 1편 = SPECTER2 벡터 1개 (청킹하지 않음).
CREATE TABLE papers (
    id            BIGSERIAL PRIMARY KEY,
    openreview_id TEXT UNIQUE NOT NULL,
    forum_id      TEXT,
    arxiv_id      TEXT,           -- arxiv_matcher가 로컬 arXiv 캐시로 채운다
    s2_paper_id   TEXT,           -- s2_enricher가 채운다
    citation_count INT,           -- S2 citationCount (보강 시점 스냅샷)

    title         TEXT NOT NULL,
    abstract      TEXT,
    keywords      TEXT[],
    primary_area  TEXT,

    venue         TEXT NOT NULL,      -- 'ICLR 2024' 형태 (투고처)
    year          INT  NOT NULL,
    -- normalize.normalize_decision() 의 출력값:
    -- accept-oral / accept-spotlight / accept-poster / accept-notable /
    -- accept / reject / withdrawn / desk-reject / unknown
    decision      TEXT NOT NULL DEFAULT 'unknown',
    final_venue   TEXT,               -- 재투고 추적으로 확인된 최종 게재처

    meta_review   TEXT,
    embedding     vector(768),        -- SPECTER2, L2 정규화 상태로 저장
    tsv           tsvector GENERATED ALWAYS AS (
                      to_tsvector('english',
                          coalesce(title, '') || ' ' || coalesce(abstract, ''))
                  ) STORED,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX papers_tsv_idx      ON papers USING gin (tsv);
CREATE INDEX papers_venue_year   ON papers (venue, year);
CREATE INDEX papers_decision     ON papers (decision);
CREATE INDEX papers_arxiv        ON papers (arxiv_id) WHERE arxiv_id IS NOT NULL;
CREATE INDEX papers_s2           ON papers (s2_paper_id) WHERE s2_paper_id IS NOT NULL;
CREATE INDEX papers_title_trgm   ON papers USING gin (title gin_trgm_ops);

-- 벡터 인덱스는 데이터 적재 후 생성하는 것이 훨씬 빠르다.
-- 43,515편 적재를 마친 뒤 scripts/build_indexes.sql 로 생성할 것.
-- (빈 테이블에 만들어두면 적재 중 인덱스 갱신 비용이 계속 발생한다.)

-- --------------------------------------------------------------- authors
CREATE TABLE authors (
    id            BIGSERIAL PRIMARY KEY,
    openreview_id TEXT UNIQUE,        -- '~Alice_Kim1' 형태
    s2_author_id  TEXT,
    name          TEXT
);
CREATE INDEX authors_s2 ON authors (s2_author_id) WHERE s2_author_id IS NOT NULL;

CREATE TABLE paper_authors (
    paper_id  BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    author_id BIGINT NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    position  INT,
    PRIMARY KEY (paper_id, author_id)
);
CREATE INDEX paper_authors_author ON paper_authors (author_id);

-- --------------------------------------------------------------- reviews
-- 정규화된 리뷰. venue×연도별 필드 차이는 ingest/normalize.py 가 흡수한다.
CREATE TABLE reviews (
    id              BIGSERIAL PRIMARY KEY,
    paper_id        BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    openreview_id   TEXT UNIQUE NOT NULL,

    rating          REAL,             -- 선두 숫자만 파싱한 값
    rating_raw      TEXT,             -- '8: Accept' 등 원문 (형식이 연도마다 다름)
    confidence      REAL,

    summary         TEXT,
    strengths       TEXT,
    weaknesses      TEXT,             -- 강/약 미분리 venue는 리뷰 본문 전체가 들어온다
    questions       TEXT,

    -- true면 강점/약점이 분리되지 않은 형식(2023년 이전)이라
    -- LLM에 본문 전체를 넘겨 분리부터 시켜야 한다. 토큰 비용 차이가 크다.
    needs_llm_split BOOLEAN NOT NULL DEFAULT false,

    raw_content     JSONB,            -- 원본 보존 (스키마 변동 대비)
    points_extracted BOOLEAN NOT NULL DEFAULT false  -- 지적항목 추출 완료 여부
);
CREATE INDEX reviews_paper   ON reviews (paper_id);
CREATE INDEX reviews_pending ON reviews (points_extracted) WHERE NOT points_extracted;

-- ---------------------------------------------------------- review_points
-- 리뷰에서 LLM으로 추출한 개별 지적 항목. 패턴 클러스터링의 단위.
CREATE TABLE review_points (
    id        BIGSERIAL PRIMARY KEY,
    review_id BIGINT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    paper_id  BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,

    -- 통제된 분류 체계 (자유 생성 금지 — 집계 품질을 위해)
    -- novelty / experimental_scale / baselines / clarity /
    -- theoretical_soundness / reproducibility / related_work /
    -- significance / other
    aspect    TEXT NOT NULL,
    -- weakness / strength / question / unknown
    -- unknown: 강약점 미분리 리뷰인데 약점 섹션 머리말도 없어 지적인지 확정
    -- 불가한 문장. 집계에서 제외한다 (review_extractor.split_weakness_section).
    sentiment TEXT NOT NULL,
    text      TEXT NOT NULL,          -- 1~2문장 요약
    embedding vector(768)
);
CREATE INDEX review_points_paper  ON review_points (paper_id);
CREATE INDEX review_points_aspect ON review_points (aspect, sentiment);

-- ------------------------------------------------------ aspect_base_rates
-- 코퍼스 전체에서 각 aspect가 지적되는 비율. 쿼리 시점 리뷰 패턴의 lift 분모다.
-- 빈도만 세면 base rate가 높은 aspect(baselines 78.8%)가 항상 1등이 되어
-- 정보량이 0이 된다 (설계서 §18). 수집 완료 후 scripts/build_base_rates.py로 채운다.
CREATE TABLE aspect_base_rates (
    aspect       TEXT NOT NULL,
    sentiment    TEXT NOT NULL,      -- weakness / strength / question
    paper_count  BIGINT NOT NULL,    -- 이 aspect를 지적받은 논문 수
    total_papers BIGINT NOT NULL,    -- 해당 sentiment의 지적이 추출된 논문 총수
    base_rate    REAL   NOT NULL,    -- paper_count / total_papers
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (aspect, sentiment)
);

-- ----------------------------------------------------------- venue_stats
-- venue별 rating 기준선과 당락 경계. 원점수는 척도가 venue마다 달라(ICLR 2020은
-- 1~8) 단독으로 보여줄 수 없고, 항상 이 기준선 대비로 환산한다 (설계서 §19).
-- is_coverage_biased: NeurIPS는 코퍼스의 95%가 accept다 — OpenReview가 채택 논문
-- 위주로만 공개하기 때문이며 실제 채택률(~25%)이 아니다. 이 플래그가 선 venue는
-- accept율 절대값과 당락 경계를 신뢰하지 않는다.
-- 수집 완료 후 scripts/build_venue_stats.py로 채운다.
CREATE TABLE venue_stats (
    venue               TEXT PRIMARY KEY,
    papers              BIGINT NOT NULL,
    accept_count        BIGINT NOT NULL,
    reject_count        BIGINT NOT NULL,
    accept_rate         REAL   NOT NULL,
    rating_mean         REAL,
    rating_sd           REAL,
    accept_rating_mean  REAL,
    reject_rating_mean  REAL,
    scale_max           REAL,             -- 관측된 최대 점수 (척도 구분용)
    threshold_50        REAL,             -- 통과율 50%를 넘기는 최저 평균 rating
    is_coverage_biased  BOOLEAN NOT NULL DEFAULT false,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------- citations
-- 그래프 DB 대신 단순 엣지 테이블. 다중 홉 순회 기능이 없어 이것으로 충분하다.
CREATE TABLE citations (
    citing_paper_id BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    cited_paper_id  BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    PRIMARY KEY (citing_paper_id, cited_paper_id)
);
CREATE INDEX citations_cited ON citations (cited_paper_id);

-- ------------------------------------------------------ submission_links
-- 같은 논문의 복수 투고 기록 연결 (ICLR reject → NeurIPS accept 추적).
CREATE TABLE submission_links (
    earlier_paper_id BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    later_paper_id   BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    match_method     TEXT NOT NULL,   -- arxiv_id / title_exact / title_author_fuzzy
    confidence       REAL NOT NULL,
    PRIMARY KEY (earlier_paper_id, later_paper_id)
);

-- ----------------------------------------------------------- paper_stories
-- 심사 서사(query/story.py) 캐시. 응답 하나에 OpenReview API 2콜 + (켜져 있으면)
-- LLM 1콜이 들어가는데, 심사가 끝난 논문은 리뷰도 수정 이력도 더 바뀌지 않는다.
-- payload는 PaperStory를 그대로 직렬화한 것이라 모델이 바뀌면 버리고 다시 만든다.
-- used_llm: 스텁으로 만든 캐시가 LLM 켠 요청에 나가지 않도록 컬럼으로 뺐다.
-- ⚠️ alembic 0009와 같은 정의다. 한쪽만 고치면 신규 컨테이너와 운영 DB가 어긋난다.
CREATE TABLE paper_stories (
    paper_id BIGINT PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    payload  JSONB       NOT NULL,
    used_llm BOOLEAN     NOT NULL DEFAULT false,
    built_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------- ingest_status
-- 수집 재시작을 위한 체크포인트. 43,515편을 며칠에 걸쳐 나눠 받는다.
CREATE TABLE ingest_status (
    venue      TEXT NOT NULL,
    stage      TEXT NOT NULL,         -- fetch / embed / extract_points / link
    status     TEXT NOT NULL,         -- pending / running / done / failed
    processed  INT  NOT NULL DEFAULT 0,
    total      INT,
    error      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (venue, stage)
);
