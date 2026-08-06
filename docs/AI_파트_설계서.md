# AI 파트 상세 설계서 — ML/AI 논문 리서치 어시스턴트

> AI 파트(`paper_assistant/`)의 설계 근거·실측 수치·실패한 접근을 남기는 문서.
> "어떻게 쓰는가"가 아니라 **"왜 이렇게 됐는가"** 를 다룬다 — 실행·구조·API는
> [DEVELOPMENT.md](DEVELOPMENT.md)에 있다.
>
> AI 파트는 Python 패키지로 개발해 같은 프로세스의 FastAPI 백엔드에서 import해 쓴다
> (통합 완료, §22). 화면은 별도 저장소 [AICE-FE](https://github.com/AICE-GACHON/AICE-FE).

---

## 1. 확정된 기술 결정 사항

| 항목 | 결정 | 비고 |
|---|---|---|
| 벡터 DB | **pgvector** (Postgres) | 벡터 + 메타데이터 + 인용 엣지를 Postgres 하나로 통합 |
| 그래프 DB | **사용 안 함** | 인용 관계는 엣지 테이블로 충분. 다중 홉 순회 기능 없음 |
| 임베딩 모델 | **SPECTER2** (allenai, HuggingFace) | 학술 논문 특화. 논문 1편 = title+abstract → 벡터 1개 |
| 오케스트레이션 | **LangGraph** | 고정 DAG + 병렬 노드. LLM supervisor 라우팅 없음 |
| LLM | **Claude API** (예산 제약, §13) | 리뷰 추출은 **$0 휴리스틱 우선**, 쿼리 시점만 Claude |
| 검색 방식 | **하이브리드** | SPECTER2 벡터 + Postgres full-text, RRF로 결합 |
| 데이터 범위 | ICLR + NeurIPS **최근 5년+** (2020~) | **실측 43,515편** / 리뷰 약 15만 건 (§10) |
| 사용자 입력 | 텍스트(제목+초록) + **PDF draft 업로드** | PDF에서 제목/초록 추출 후 동일 파이프라인 |
| 리뷰 지적 항목 추출 | **오프라인 배치** (수집 시) | 쿼리 시점엔 클러스터링+집계만 |
| 유사성 근거 태깅 | **MVP 포함** | 상위 10~20편에 대해 쿼리 시점 LLM 태깅 |
| 재투고 흐름 추적 | **제대로 구현** | arXiv ID + 제목 유사도 + 저자 매칭 |
| 제공 형태 | **Python 패키지** | 백엔드도 Python. FastAPI 데모 서버는 개발용으로만 |
| 패키지 관리 | **pip + requirements.txt** | 팀 통일 |

---

## 2. 전체 아키텍처

### 2.1 두 개의 독립된 파이프라인

```
[A] 수집/인덱싱 파이프라인 (오프라인 배치, 주기적 실행)
    OpenReview API ──┐
    Semantic Scholar ─┼─→ 정규화 → LLM 리뷰 구조화 → 임베딩 → Postgres 적재
    arXiv API ────────┘

[B] 쿼리 파이프라인 (LangGraph, 사용자 요청마다 실행)
    사용자 입력 → 검색 → 병렬 분석 → 종합 리포트
```

### 2.2 쿼리 파이프라인 (LangGraph DAG)

Supervisor 패턴 대신 **고정 DAG**. 워크플로우가 매번 동일하므로 LLM 라우팅은
불필요한 비용/지연/불확실성만 추가한다. LLM은 지능이 필요한 노드 안에서만 사용.

```
        ┌─────────────────────────────┐
        │  input_node                 │  텍스트 or PDF → 제목/초록 정규화
        └──────────────┬──────────────┘
        ┌──────────────▼──────────────┐
        │  retrieval_node             │  하이브리드 검색 (벡터+FTS, RRF)
        │                             │  → 유사 논문 상위 K편 (기본 20)
        └──────┬──────────────┬───────┘
     ┌─────────▼────┐  ┌──────▼─────────┐     ← 이 3개만 병렬
     │ similarity_  │  │ review_        │  ┌────────────────┐
     │ tagging_node │  │ analysis_node  │  │ venue_trend_   │
     │ (LLM 태깅)   │  │ (클러스터링)    │  │ node (SQL 집계) │
     └─────────┬────┘  └──────┬─────────┘  └──────┬─────────┘
        ┌──────▼──────────────▼────────────────────▼───────┐
        │  synthesis_node (Sonnet)                          │
        │  → 최종 구조화 리포트 (JSON + 마크다운 요약)        │
        └───────────────────────────────────────────────────┘
```

**노드별 역할**

| 노드 | LLM 사용 | 내용 |
|---|---|---|
| `input_node` | PDF일 때만 (Haiku) | PDF → PyMuPDF로 텍스트 추출 → Haiku로 제목/초록 식별. 텍스트 입력이면 통과 |
| `retrieval_node` | 없음 | SPECTER2 임베딩 → pgvector 코사인 검색 + Postgres `tsvector` full-text 검색 → RRF(Reciprocal Rank Fusion) 결합 → 상위 K편 |
| `similarity_tagging_node` | Haiku | 상위 10~20편 각각에 대해 "왜 유사한가" 태깅: `methodology` / `dataset` / `problem_setting` / `citation` + 한 줄 근거. 논문당 1콜, 병렬 호출 |
| `review_analysis_node` | 없음 (임베딩만) | 유사 논문들의 **사전 추출된 지적 항목**을 DB에서 로드 → 임베딩 기반 클러스터링(HDBSCAN 또는 agglomerative) → "10편 중 6편이 실험 규모 지적" 형태로 집계 |
| `venue_trend_node` | 없음 | SQL 집계: **학회 단위**(ICLR/NeurIPS) accept 비율 + 재투고 흐름. 연도별로 쪼개면 셀당 1~3편이라 표본이 무의미 → `split_part`로 연도 떼고 학회로 합침 (§14.6). 재투고 흐름은 연도까지 유지 |
| `synthesis_node` | Sonnet | 세 분석 결과를 받아 사람이 읽는 종합 리포트 생성. 구조화 JSON도 함께 반환 (프론트가 컴포넌트별 렌더링 가능하도록) |

### 2.3 수집/인덱싱 파이프라인 (배치)

5년치(5만+ 편)이므로 **재시작 가능(체크포인트)** 설계가 필수.

```
1. fetch_openreview   : venue×연도 단위로 논문+리뷰+메타리뷰+rebuttal+decision 수집
2. fetch_arxiv        : arXiv OAI-PMH 하베스트 → 제목 매칭으로 arXiv ID 확정 (§20)
3. fetch_s2           : Semantic Scholar에서 s2_paper_id, 인용수/인용 관계, 저자 ID,
                        최종 게재처 보강 (arXiv ID를 키로 batch 조회) (§20)
4. extract_review_points : 리뷰 → Haiku → 구조화된 지적 항목 리스트 (아래 §4)
5. link_submissions   : 재투고 흐름 매칭 (아래 §6)
6. embed              : SPECTER2로 논문/지적항목 임베딩
7. load               : Postgres 적재 (upsert, 단계별 상태 컬럼으로 체크포인트)
```

- 각 단계는 독립 실행 가능한 스크립트 + `ingest_status` 테이블로 진행 상태 추적
- OpenReview API v2는 rate limit 존재 → 지수 백오프 + venue×연도 단위 체크포인트
- LLM 비용: 리뷰 지적 항목 추출이 대부분. 리뷰 ~20만 건 × Haiku ≈ 감당 가능한 수준이지만, **1개 venue×연도로 먼저 파일럿 실행해서 편당 비용 측정 후 전체 실행**

---

## 3. 데이터 스키마 (Postgres + pgvector)

```sql
-- 논문 (검색의 기본 단위)
CREATE TABLE papers (
    id              BIGSERIAL PRIMARY KEY,
    openreview_id   TEXT UNIQUE,
    arxiv_id        TEXT,
    s2_paper_id     TEXT,
    title           TEXT NOT NULL,
    abstract        TEXT,
    venue           TEXT,          -- 'ICLR', 'NeurIPS'
    year            INT,
    decision        TEXT,          -- 'accept-oral', 'accept-poster', 'reject', 'withdrawn'
    final_venue     TEXT,          -- 최종 게재처 (재투고 추적 결과)
    embedding       vector(768),   -- SPECTER2
    tsv             tsvector GENERATED ALWAYS AS
                      (to_tsvector('english', title || ' ' || coalesce(abstract,''))) STORED
);
CREATE INDEX ON papers USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON papers USING gin (tsv);

-- 저자 (재투고 매칭용)
CREATE TABLE authors (
    id           BIGSERIAL PRIMARY KEY,
    s2_author_id TEXT UNIQUE,
    name         TEXT
);
CREATE TABLE paper_authors (
    paper_id  BIGINT REFERENCES papers(id),
    author_id BIGINT REFERENCES authors(id),
    position  INT,
    PRIMARY KEY (paper_id, author_id)
);

-- 리뷰 원문
CREATE TABLE reviews (
    id            BIGSERIAL PRIMARY KEY,
    paper_id      BIGINT REFERENCES papers(id),
    openreview_id TEXT UNIQUE,
    review_type   TEXT,   -- 'review', 'meta_review', 'rebuttal', 'decision'
    rating        TEXT,
    confidence    TEXT,
    content       JSONB   -- OpenReview 원본 필드 보존
);

-- 사전 추출된 리뷰 지적 항목 (클러스터링의 단위)
CREATE TABLE review_points (
    id         BIGSERIAL PRIMARY KEY,
    review_id  BIGINT REFERENCES reviews(id),
    paper_id   BIGINT REFERENCES papers(id),
    aspect     TEXT,        -- 통제된 분류 (§4)
    sentiment  TEXT,        -- 'weakness', 'strength', 'question'
    text       TEXT,        -- 지적 내용 요약 (1~2문장)
    embedding  vector(768)
);
CREATE INDEX ON review_points USING hnsw (embedding vector_cosine_ops);

-- 인용 엣지 (그래프 DB 대체)
CREATE TABLE citations (
    citing_paper_id BIGINT REFERENCES papers(id),
    cited_paper_id  BIGINT REFERENCES papers(id),
    PRIMARY KEY (citing_paper_id, cited_paper_id)
);

-- 재투고 연결 (같은 논문의 복수 투고 기록)
CREATE TABLE submission_links (
    earlier_paper_id BIGINT REFERENCES papers(id),
    later_paper_id   BIGINT REFERENCES papers(id),
    match_method     TEXT,      -- 'arxiv_id', 'title_exact', 'title_author_fuzzy'
    confidence       REAL,
    PRIMARY KEY (earlier_paper_id, later_paper_id)
);

-- 수집 체크포인트
CREATE TABLE ingest_status (
    venue TEXT, year INT, stage TEXT, status TEXT, updated_at TIMESTAMPTZ,
    PRIMARY KEY (venue, year, stage)
);
```

---

## 4. 청킹/임베딩 전략

**핵심: 검색 대상별로 단위가 다르다. 논문 유사도 검색에는 청킹이 없다.**

| 대상 | 단위 | 모델 | 이유 |
|---|---|---|---|
| 논문 유사도 검색 | **논문 1편 = 벡터 1개** (title + `[SEP]` + abstract) | SPECTER2 (proximity adapter) | SPECTER2가 정확히 이 용도로 학습됨. 본문 청킹은 노이즈만 추가 |
| 리뷰 지적 패턴 | **지적 항목 1개 = 벡터 1개** | SPECTER2 base (또는 동일 모델 통일) | 리뷰 전체 임베딩은 여러 주제가 섞여 클러스터링 품질 저하. LLM으로 항목 분리 후 임베딩 |
| 본문 full-text | **MVP 제외** | — | Phase 3 "예상 지적 예측" 때 섹션 단위로 추가 |

**리뷰 지적 항목 추출 (오프라인 배치, Haiku)**

리뷰 1건 → 아래 형태의 리스트로 구조화:

```json
[
  {"aspect": "experimental_scale", "sentiment": "weakness",
   "text": "Experiments limited to CIFAR-10/100; no ImageNet-scale validation."},
  {"aspect": "novelty", "sentiment": "weakness",
   "text": "Method is incremental over prior work X."}
]
```

`aspect`는 자유 생성이 아니라 **통제된 분류 체계**를 프롬프트에 명시 (클러스터링·집계 품질을 위해):
`novelty` / `experimental_scale` / `baselines` / `clarity` / `theoretical_soundness` / `reproducibility` / `related_work` / `significance` / `other`

쿼리 시점 클러스터링은 이 aspect 1차 그룹핑 + 임베딩 유사도 2차 병합으로 "유사 논문 10편 중 6편이 실험 규모 지적" 형태 집계 생성.

---

## 5. 하이브리드 검색 상세

```
score = RRF(vector_rank, fts_rank)   # 1/(60+rank) 합산, 표준 RRF
```

1. SPECTER2로 쿼리(제목+초록) 임베딩 → pgvector 코사인 top-50
2. Postgres `ts_rank` full-text top-50 (특정 데이터셋명·기법명 정확 매칭 보완)
3. RRF 결합 → 상위 K=20편 반환
4. 상위 20편만 similarity_tagging_node로 전달

전부 Postgres 쿼리 1~2개로 처리 가능. 별도 검색 엔진 불필요.

### ⚠️ full-text 쿼리는 반드시 OR 결합할 것 (실측으로 발견한 함정)

`plainto_tsquery`는 입력의 **모든 단어를 AND로 결합**한다. 사용자 입력이 초록
전체(수백 단어)이므로 그 단어를 전부 포함하는 문서만 걸린다 —
**실측: 200편 중 1편만 매칭되어 FTS가 사실상 무력화됐다.**

```sql
-- 잘못됨: 초록 전체를 넣으면 AND 조건이 되어 거의 매칭되지 않음
WHERE tsv @@ plainto_tsquery('english', :query)

-- 올바름: lexeme을 OR로 결합해 ts_rank가 겹치는 정도로 순위를 매김
WITH q AS (SELECT to_tsquery('english', string_agg(lexeme, ' | ')) AS query
           FROM unnest(to_tsvector('english', :query)))
SELECT p.id FROM papers p, q WHERE p.tsv @@ q.query
ORDER BY ts_rank(p.tsv, q.query) DESC
```

수정 후 같은 조건에서 200/200편 매칭되며, 벡터 검색에서 9~10위였던 논문이
키워드 매칭 덕에 3~4위로 올라오는 **하이브리드 본래의 동작**이 확인됐다.
회귀 방지 테스트: `tests/test_db_integration.py::test_fulltext_or_matching_beats_and_matching`

---

## 6. 재투고 흐름 매칭 (제대로 구현)

우선순위 폴백 체인:

1. **arXiv ID 일치** — 다른 venue 투고 기록이 같은 arXiv ID를 가리키면 확정 (confidence 1.0)
2. **제목 정확 일치** (정규화 후: 소문자, 공백/특수문자 정리) — confidence 0.95
3. **제목 유사 + 저자 겹침** — 제목 임베딩(또는 문자열 유사도 ≥ 임계값) AND 저자 집합 Jaccard ≥ 0.5 → confidence 산출, 임계값 이하 폐기

결과는 `submission_links`에 저장하고 venue_trend_node에서
"이 유형 논문의 재투고 흐름: ICLR'24 reject → NeurIPS'24 accept 12건" 형태로 집계.
저자 매칭을 위해 Semantic Scholar **author ID를 수집 단계에서 반드시 저장**.

---

## 7. 패키지 구조

```
paper_assistant/               # pip install -e . 로 백엔드에서 import
├── requirements.txt
├── setup.py (or pyproject.toml)
├── paper_assistant/
│   ├── __init__.py            # 공개 API: analyze(query) -> Report
│   ├── config.py              # DB URL, API 키 (환경변수)
│   ├── ingest/                # 파이프라인 A (배치)
│   │   ├── openreview_client.py
│   │   ├── s2_client.py
│   │   ├── arxiv_client.py
│   │   ├── review_extractor.py    # Haiku 지적항목 추출
│   │   ├── submission_linker.py   # 재투고 매칭
│   │   └── run_ingest.py          # CLI 엔트리포인트 (체크포인트 재개)
│   ├── embedding/
│   │   └── specter2.py
│   ├── retrieval/
│   │   └── hybrid_search.py       # pgvector + FTS + RRF
│   ├── graph/                 # 파이프라인 B (LangGraph)
│   │   ├── state.py               # TypedDict 상태 정의
│   │   ├── nodes.py               # 6개 노드
│   │   └── pipeline.py            # DAG 조립
│   ├── pdf/
│   │   └── extract.py             # PyMuPDF + Haiku 제목/초록 추출
│   └── schemas.py             # Pydantic: Report, SimilarPaper, ReviewPattern, VenueTrend
├── scripts/
│   └── init_db.sql
├── demo_server/               # 개발용 FastAPI (통합 전 데모/테스트)
│   └── main.py
└── tests/
```

**백엔드 통합 계약(contract)**: 공개 API는 단 하나 —
`paper_assistant.analyze(title, abstract, pdf_bytes=None) -> Report` (Pydantic 모델).
백엔드 팀은 이 함수 시그니처와 `Report` 스키마만 알면 됨. 스트리밍이 필요해지면
LangGraph의 `astream()`을 그대로 노출하는 `analyze_stream()` 추가.

---

## 8. 구현 순서 (AI 파트 로드맵)

1. ~~**주차 1 — 데이터 파일럿**~~ **(진행 중)**: OpenReview API 탐색 ✅, 정규화 레이어 + 10개 venue 검증 ✅ → 남은 것: 리뷰 추출 프롬프트 튜닝 + 편당 LLM 비용 측정 (Anthropic 키 필요)
2. ~~**주차 2 — 검색 코어**~~ **✅ 완료** (§11, §12): SPECTER2 임베딩 + pgvector 적재 + 하이브리드 검색, 200편 end-to-end 검증 통과
3. ~~**주차 3 — LangGraph 파이프라인**~~ **✅ 완료** (§14): 6개 노드 조립, 병렬 분석, $0 배선 검증. 남은 것: 재투고 매칭·PDF·Haiku 태깅 실측
4. ~~**주차 4 — 전체 수집**~~ **✅ 완료**: 43,515편 적재(710분) + HNSW 인덱스 + 재투고 매칭 744건(ICLR↔NeurIPS reject→accept 흐름 확인)
5. **주차 5 — 마감**: PDF 입력, demo_server, Report 스키마 문서화 → 백엔드 팀 전달

---

## 9. 리스크 (AI 파트 한정)

- ~~**SPECTER2 차원 확인**~~ → **해소됨**. 실측 768차원 확정 (§11). 스키마의 `vector(768)` 그대로 사용
- ~~**OpenReview 스키마 변동**~~ → **해소됨**. 실측 결과 §10 참고. 정규화 레이어(`ingest/normalize.py`)로 흡수 완료, 10개 venue 검증 통과
- **리뷰 추출 품질**: aspect 분류가 흔들리면 클러스터링 전체가 흔들림 → 파일럿 단계에서 수동 라벨 50건과 비교 검증
- **Claude API 비용**: 수집 단계가 지배적. 파일럿에서 실측 후 전체 실행 여부 판단

---

## 10. OpenReview API 실측 결과 (2026-07-21 조사)

기획 단계의 추정이 아니라 **실제 API를 호출해 확인한 사실**. 수집 파이프라인 구현의 근거.

### 10.1 API 버전이 두 개로 갈린다

2023년 전후로 OpenReview가 API를 교체했고, **구 venue는 v2에서 조회되지 않는다.**

| venue | API | submission invitation | 논문 수 |
|---|---|---|---|
| ICLR 2020 | v1 | `-/Blind_Submission` | 2,213 |
| ICLR 2021 | v1 | `-/Blind_Submission` | 2,594 |
| ICLR 2022 | v1 | `-/Blind_Submission` | 2,617 |
| ICLR 2023 | v1 | `-/Blind_Submission` | 3,792 |
| ICLR 2024 | v2 | `-/Submission` | 7,404 |
| ICLR 2025 | v2 | `-/Submission` | 11,672 |
| NeurIPS 2021 | v1 | `-/Blind_Submission` | 2,768 |
| NeurIPS 2022 | v1 | `-/Blind_Submission` | 2,824 |
| NeurIPS 2023 | v2 | `-/Submission` | 3,395 |
| NeurIPS 2024 | v2 | `-/Submission` | 4,236 |
| **합계** | | | **43,515** |

리뷰 추정 약 **15만 건** (편당 3.5건). v1은 `api.openreview.net`, v2는 `api2.openreview.net`.
v2는 모든 content 필드를 `{"value": x}`로 감싸지만 v1은 raw 값 — 정규화 레이어에서 흡수.

### 10.2 인증·요청 관련 함정 (전부 실측으로 확인)

- **익명 `/notes` 요청은 403** (`ChallengeRequiredError`, 봇 검증) → **로그인 필수**. v1/v2 모두 동일
- **`/login` 자체에 rate limit** 존재 → 토큰을 디스크에 캐시해 재사용 (`data/.token_*.json`, JWT `exp` 검사)
- **v2는 `limit=1`이면 캐시 응답을 주고 `count` 필드를 생략** → 총 개수를 알려면 `limit>=3` + `offset` 명시
- 공식 `openreview-py` 라이브러리는 의존성(`editdistance`)이 **Python 3.14 휠 미제공**으로 설치 실패 → raw REST로 직접 구현 (의존성도 가볍고 체크포인트 제어도 쉬움)

### 10.3 리뷰 필드가 venue×연도마다 전부 다르다

**이것이 최대 함정이었다.** 같은 ICLR인데도 연도마다 필드명·점수 형식이 바뀐다.

| venue | 리뷰 본문 필드 | 점수 필드 | 강점/약점 분리 |
|---|---|---|---|
| ICLR 2020 | `review` | `rating` | ❌ 통짜 |
| ICLR 2021 | `review` | `rating` | ❌ 통짜 |
| ICLR 2022 | `main_review` | `recommendation` | ❌ 통짜 |
| ICLR 2023 | `strength_and_weaknesses` | `recommendation` | △ 합쳐짐 |
| ICLR 2024/2025 | `strengths` + `weaknesses` | `rating` | ✅ 분리 |
| NeurIPS 2021 | `main_review` | `rating` | ❌ 통짜 |
| NeurIPS 2022 | `strengths_and_weaknesses` | `rating` | △ 합쳐짐 |
| NeurIPS 2023/2024 | `strengths` + `weaknesses` | `rating` | ✅ 분리 |

**점수 형식도 제각각**: `"8: Accept"`, `"5"`, `"3: reject, not good enough"`, `"2 fair"` → 선두 숫자 파싱으로 통일.

**설계에 미치는 영향**: 2024년 이후 venue는 `weaknesses` 필드만 LLM에 넘기면 되지만,
그 이전은 리뷰 본문 전체를 넘겨 강점/약점부터 분리해야 한다. `NormalizedReview.needs_llm_split`
플래그로 구분하고 `llm_input` 프로퍼티가 최소 토큰만 반환하도록 설계 → **LLM 비용 절감**.

### 10.4 메타리뷰 위치도 다르다

`Meta_Review` 노트가 **존재하는 venue는 ICLR 2024, NeurIPS 2022뿐**.
나머지는 `Decision` 노트의 `comment` 필드(ICLR 2023은 `metareview:_summary,_strengths_and_weaknesses`)에 들어있다.
→ 정규화 레이어에서 Meta_Review 노트 우선, 없으면 Decision 노트로 폴백.

### 10.5 decision 판별

`venue` 문자열(`"ICLR 2024 poster"`, `"Submitted to ICLR 2024"`)로 판별하는 게 1순위지만,
**ICLR 2020/2021은 submission content에 `venue` 필드 자체가 없다** → `Decision` 노트 값으로 폴백.
정규화 결과: `accept-oral` / `accept-spotlight` / `accept-poster` / `accept-notable` / `accept` /
`reject` / `withdrawn` / `desk-reject` / `unknown`.

### 10.6 검증 상태

`scripts/verify_normalize.py`로 **10개 venue × 3편**을 실제 API에서 받아 정규화 검증 →
title/abstract/decision/rating/리뷰본문/author_ids 전 항목 정상, 문제 0건.
단위 테스트 9건(`tests/test_normalize.py`)이 각 연도 형식을 회귀 방지용으로 고정.

---

## 11. SPECTER2 임베딩 실측 결과 (2026-07-21)

### 11.1 환경·성능

| 항목 | 실측값 |
|---|---|
| 임베딩 차원 | **768** (스키마 `vector(768)` 확정) |
| 처리 속도 | 편당 **69ms** (CPU) |
| 전체 43,515편 예상 | **약 0.8시간** (CPU) |
| **GPU 필요 여부** | **불필요** — CPU로 1시간 내 완료 |
| Python 3.14 호환 | torch 2.13 / transformers 4.57 / adapters 1.3 **정상 설치** |

모델 구성: `allenai/specter2_base` + proximity adapter.
입력 형식은 학습 시와 동일하게 `title + [SEP] + abstract`, 출력은 마지막 레이어 CLS 토큰을 L2 정규화.

### 11.2 유사도 스케일이 좁다 — 절대 임계값을 쓰면 안 된다

ICLR 2024 논문 **300편(무작위 쌍 89,700개)** 으로 측정한 코사인 유사도 분포:

| 통계 | 값 |
|---|---|
| 최소 / 평균 / 최대 | 0.721 / **0.845** / 0.978 |
| 표준편차 | 0.033 |
| 25 / 50 / 75 분위 | 0.823 / 0.845 / 0.867 |
| 95 / 99 / 99.9 분위 | 0.900 / 0.923 / 0.944 |

검증용 논문쌍의 위치:

| 쌍 | 코사인 | 백분위 |
|---|---|---|
| Transformer ↔ 단백질 구조 예측 (무관) | 0.844 | **55.8%** (중앙값) |
| Transformer ↔ BERT (관련) | 0.920 | 상위 1.3% |
| TabR ↔ Revisiting Tabular DL (관련) | 0.956 | 상위 0.1% |

**모델은 정상 작동한다** — 무관한 쌍을 정확히 중앙값에 놓았다.
문제는 **스케일**이다. 무작위 쌍조차 0.845가 나오므로:

1. **`0.85 이상 = 유사` 같은 절대 임계값은 무의미**하다. 검색은 반드시 **top-K 순위 기반**.
   → RRF 하이브리드(§5)가 순위 기반이라 이 특성과 잘 맞는다. 설계 선택이 검증된 셈.
2. **프론트에 원시 코사인 값을 "유사도 84%"로 노출하면 사용자가 반드시 오해한다.**
   → `similarity_percentile()`로 백분위 변환해서 전달한다 (측정된 분위수 기반 선형 보간).
   백엔드/프론트 팀에 넘길 `Report` 스키마에는 **백분위를 담고 원시 코사인은 담지 않는다.**

재측정이 필요하면 `scripts/measure_similarity_dist.py` 실행 (참조 분위수 갱신용).

---

## 12. DB 구축 및 검색 검증 결과 (2026-07-21)

### 12.1 구성

pgvector 공식 이미지(`pgvector/pgvector:pg17`)를 Docker로 기동. **포트 5433**을 쓴다
(로컬에 다른 Postgres가 있어도 충돌하지 않도록).

```bash
docker compose up -d      # 최초 기동 시 scripts/init_db.sql 자동 실행
```

테이블 8개: `papers` / `reviews` / `review_points` / `authors` / `paper_authors` /
`citations` / `submission_links` / `ingest_status`.
확장: `vector`(임베딩), `pg_trgm`(재투고 제목 매칭).

**벡터 인덱스(HNSW)는 스키마에 넣지 않았다.** 빈 테이블에 미리 만들면 적재 내내
인덱스 갱신 비용이 발생하므로, 전체 적재 후 `scripts/build_indexes.sql`로 생성한다.

### 12.2 End-to-end 검증 (ICLR 2024, 200편)

| 단계 | 실측 |
|---|---|
| 수집 + 정규화 | 200편 |
| 임베딩 (CPU) | 58초 |
| DB 적재 | 0.9초 (논문 200 + 저자 902명) |
| 하이브리드 검색 | **16ms** |

쿼리 논문 자신이 1위로 반환되고, 2~5위가 전부 같은 분야(그래프 신경망) 논문으로
채워지는 것을 확인. `tsvector`는 생성 컬럼이라 적재만 하면 자동으로 채워진다.

### 12.3 Python 3.14 관련 이슈

커넥션 풀을 명시적으로 닫지 않으면 인터프리터 종료 시
`PythonFinalizationError: cannot join thread at interpreter shutdown`이 발생한다
(3.14부터 종료 시점 스레드 join이 금지됨). `atexit.register(close_pool)`로 해결.

### 12.4 HNSW 인덱스 (완료)

전체 43,515편 적재 후 `build_indexes.sql`로 HNSW 인덱스 생성 완료(직렬 빌드 20초).

**Docker 함정**: 병렬 인덱스 빌드가 공유 메모리를 쓰는데 컨테이너 기본 `/dev/shm`
(64MB)이 작아 `could not resize shared memory segment`로 실패한다. 해결:
`SET max_parallel_maintenance_workers = 0`(직렬 빌드) + docker-compose `shm_size: 1gb`.
`review_points.embedding`은 전부 NULL(쿼리 시점 임베딩)이라 인덱스 생략.

---

## 13. LLM 비용 전략 (예산 $4.92 기준, 2026-07-22)

### 13.1 전체 추출은 예산 밖

리뷰 15만 건을 Haiku로 지적항목 추출하면 정가 **약 $200** (Batch 50%로도 $100).
가용 예산 $4.92로는 전체의 5%도 못 돌린다. 실측 기반 추정:

| 항목 | 리뷰당 토큰 | 15만 건 |
|---|---|---|
| 입력 (weakness ~350 + 프롬프트 ~250) | ~600 | 90M → $90 |
| 출력 (JSON ~150) | ~150 | 22M → $110 |

가격: Haiku 4.5 = 입력 $1 / 출력 $5 per 1M. Sonnet 5 = $2/$10 (2026-08-31까지 인트로).

### 13.2 결정: $0 휴리스틱 추출을 기본값으로

수집 단계 LLM 비용을 **$0으로** 만들고, 예산은 쿼리 시점 데모 콜(태깅·종합)에만 쓴다.

- **`HeuristicExtractor`** (`ingest/review_extractor.py`): 리뷰를 불릿/번호/문장 단위로
  분리하고 키워드로 aspect를 근사. LLM 불필요.
- **`HaikuExtractor`**: 동일 인터페이스(`PointExtractor`)의 플레이스홀더. 품질이
  부족하면 수집 스크립트에서 extractor만 교체하면 되고 다운스트림은 그대로.

### 13.3 실측 품질 (ICLR 2024, 100편 / 리뷰 384건)

| 지표 | 값 | 평가 |
|---|---|---|
| 리뷰당 지적항목 수 | 평균 **7.4개** | ✅ 분리 우수 |
| aspect `other` 비율 | **68%** | ⚠️ 키워드 분류는 약함 |

**핵심 판단**: `other` 68%는 병목이 아니다. "유사 논문 N편 중 M편이 비슷한 지적"
기능은 **aspect 라벨이 아니라 지적항목 텍스트의 임베딩 클러스터링**으로 생성되며,
클러스터는 대표 문장(medoid)으로 라벨링된다. 즉 aspect가 `other`여도 그 항목은
임베딩되어 정상적으로 클러스터링된다. aspect는 보조 필터일 뿐이다.

→ **$0 경로로 리뷰 패턴 분석 기능이 성립한다.** Haiku가 개선하는 것은 aspect 라벨의
정확도뿐이며(있으면 좋은 정도), 핵심 기능을 막지 않는다.

### 13.4 예산 사용 계획

1. 수집 + 임베딩: 전체 43,515편, **$0** (LLM 미사용)
2. 리뷰 지적항목: 전체 **$0** (휴리스틱)
3. 예산 $4.92: 쿼리 시점 태깅(Haiku) + 종합 리포트(Sonnet) 데모 콜 전용
   (쿼리당 ~$0.05 → 약 90~100회 데모 가능)

풀스케일 Haiku 추출($100)은 예산 확보 시 Batch로 하룻밤 실행하는 향후 과제로 남긴다.

---

## 14. LangGraph 파이프라인 구현 결과 (2026-07-22)

### 14.1 구조

설계 §2.2의 고정 DAG를 LangGraph로 구현. supervisor 없음.

```
input → retrieval → ┬ similarity_tagging (Haiku) ┐
                    ├ review_analysis   (no LLM) ┼→ synthesis (Sonnet) → END
                    └ venue_trend       (no LLM) ┘
```

검색 이후 3개 분석 노드가 **병렬**, synthesis는 fan-in. 200편 파일럿에서
end-to-end 정상 작동 확인.

### 14.2 예산 안전장치: LLM 토글

`get_llm(enabled=False)`(기본)면 태깅·종합 노드가 **결정론적 스텁**을 만든다.
→ 크레딧 0원으로 DAG 배선·스키마를 전부 검증 가능. 데모 때만
`PAPER_ASSISTANT_USE_LLM=1`로 실제 Claude(Haiku/Sonnet) 호출.

### 14.3 ⚠️ SPECTER2는 리뷰 문장 클러스터링에 부적합 (설계 변경)

설계 §4는 지적항목을 임베딩→클러스터링하려 했으나, **SPECTER2로 짧은 리뷰
문장을 임베딩하니 유사도가 평균 0.872에 압축**된다 (논문 title+abstract용
모델이라 짧은 문장을 변별 못 함). 실측: ICLR 2020 지적항목 400개, 무작위 쌍
유사도 50분위 0.873 / 95분위 0.925. **임계값 0.80에서도 17편이 한 클러스터로 뭉침** →
클러스터링 무의미.

**대응**: 임베딩 클러스터링 대신 **키워드 aspect 기반 집계**를 1차 방법으로 채택.
`review_analysis_node`는 지적항목을 aspect별로 묶어 "20편 중 12편이 명확성 지적,
12편 baselines, 10편 신규성 지적" 형태로 집계한다. 장점:

- **쿼리 시점 임베딩 불필요** → 더 빠르고 단순 (§13에서 리뷰 임베딩을 미룬 결정과 부합)
- 해석 가능하고 정직 (임베딩 클러스터의 애매한 medoid 라벨보다 명확)
- `HeuristicExtractor`의 aspect 68% other여도, 나머지 32%가 깔끔한 패턴을 만듦

`clustering.py._greedy_cluster`는 범용 유틸로 남겨둠 (향후 aspect 내부 세분화 등).

### 14.4 백엔드 통합 계약 확정

```python
from paper_assistant import analyze
report = analyze(title, abstract, pdf_bytes=None) -> Report   # Pydantic
```

`Report`(`schemas.py`)는 similar_papers / review_patterns / venue_trends /
resubmission_flows / summary_markdown로 구성. **원시 코사인은 담지 않고
similarity_percentile만** 담는다 (§11.2). JSON 직렬화 왕복 테스트 통과.

### 14.5 남은 것

- ~~**재투고 흐름**~~ **✅ 완료** (§15)
- ~~**PDF 입력**~~ **✅ 완료** (§16) — PyMuPDF + 폰트 크기 기반 제목 추출(저자 배제)
- ~~**similarity_tagging 실측**~~ **✅ 완료** (§17) — LSTM 쿼리로 실제 Haiku+Sonnet 검증

### 14.6 게재 경향은 학회 단위 집계 (LLM 실측 후 수정)

초기엔 venue(ICLR 2024)별로 집계했으나, 유사 논문 20편을 venue×연도로 나누면
셀당 1~3편이라 accept율이 통계적으로 무의미했다(LSTM 쿼리 실측: "NeurIPS 2023 3/3"
같은 표본). `split_part(venue,' ',1)`로 학회 단위(ICLR/NeurIPS)로 합쳐 표본을 키움
→ "ICLR 4/15(27%) vs NeurIPS 4/5(80%)"처럼 의미 있는 비율. 연도 정보는
재투고 흐름(resubmission_flows)에서 유지된다.

---

## 15. 재투고 매칭 구현 (2026-07-22)

### 15.1 폴백 체인 (`ingest/submission_linker.py`)

| 순위 | 방법 | confidence | 상태 |
|---|---|---|---|
| 1 | arXiv ID 일치 | 1.00 | ✅ 작동 (§20의 arXiv 매칭으로 arxiv_id가 채워진 뒤 활성화됨) |
| 2 | 정규화 제목 정확 일치 | 0.95 | ✅ 작동 |
| 3 | 제목 유사(pg_trgm ≥ 0.7) + 저자 Jaccard ≥ 0.5 | = 제목 유사도 | ✅ 작동 |

- 제목 정규화: 소문자 + 영숫자 외 제거 + 공백 정리
- 방향: `venue_sort_key`로 정렬 — 같은 해면 ICLR(상반기) < NeurIPS(하반기).
  → "ICLR 2024 reject → NeurIPS 2024 accept" 흐름이 올바르게 생성됨
- 전량 재계산(TRUNCATE + insert) 멱등. 전체 수집 완료 후 재실행하면 커버리지 상승
- 결과는 `venue_trend_node`가 유사 논문 집합에 대해 집계 → `Report.resubmission_flows`

### 15.2 검증

부분 데이터(ICLR 2020/2021)에서 실제 재투고 정확히 포착:
**"Towards Finding Longer Proofs" ICLR 2020 reject → ICLR 2021 reject** (title_exact, 0.95).
NeurIPS venue 적재 후 ICLR↔NeurIPS 흐름이 다수 잡힐 것으로 예상.

### 15.3 psycopg 함정 (실측)

- `set_limit(0.7)` → `set_limit(0.7::real)` 캐스트 필요 (double precision 거부)
- trgm `%` 연산자는 SQL에서 `%%`로 쓰되 **빈 파라미터 `()`를 넘겨야** psycopg가 축약

### 15.4 NUL 바이트 (수집 중 발견)

일부 논문 초록/리뷰에 NUL(0x00) 바이트 → Postgres text 컬럼이 거부(ICLR 2021에서 발생).
`normalize.clean_text()`가 제어 문자를 제거(탭·개행 보존)하도록 수정. 수집 재개.

---

## 16. PDF 입력 + 데모 웹 (2026-07-22)

### 16.1 PDF 입력 (`pdf/extract.py`) — 실제 기능

`analyze(pdf_bytes=...)` 지원. PyMuPDF로 첫 2페이지를 읽고,
**제목은 폰트 크기로, 초록은 텍스트 마커로** 뽑는다. `input_node`에 연결됨.

**제목을 폰트 크기로 뽑는 이유 (실측)**: 텍스트 순서만 보면 제목 바로 뒤에 붙는
저자 줄을 걸러낼 수 없다 — 실제로 "Agentic Business Process Management: A Research
Manifesto **Diego Calvanese, Angelo Casciani, ...**"처럼 저자가 제목에 섞여 나왔다.
논문 첫 페이지에서 제목은 항상 본문보다 큰 폰트다(실측: 제목 14.3~17.2pt vs 저자 10pt).
최대 폰트 크기의 **75% 이상**인 span만 채택한다.

- 임계값이 95%가 아니라 75%인 이유: **드롭캡 조판**(ICLR 스타일)은 첫 글자만
  17.2pt이고 나머지는 13.8pt라, 좁게 잡으면 `"R N N R"`처럼 첫 글자만 남는다.
- 드롭캡으로 분리된 `"R ECURRENT"`는 정규식(`[A-Z] [A-Z]{2,}`)으로 다시 붙인다.
- arXiv 세로 헤더(20pt)가 제목보다 클 수 있어 `_HEADER_CRUFT`로 먼저 제외한다.

검증(실제 PDF 3종): `Agentic Business Process Management: A Research Manifesto`,
`ImageNet Classification with Deep Convolutional Neural Networks`,
`RECURRENT NEURAL NETWORK REGULARIZATION` — 모두 저자 없이 정확히 추출.

**한계 — 1990년대 TeX PDF는 지원하지 않음**: LSTM(1997) 같은 옛날 PDF는 글자가
물리적으로 쪼개져 저장되고(`Ho`+`c`+`hreiter`), 합자가 표준 유니코드가 아닌 폰트
내부 코드(`\x0e`=ffi, `\x0d`=fl)라 **어떤 추출기로도 복원이 어렵다**(pypdf·PyMuPDF
모두 실패 확인). 실사용자는 최신 논문을 쓰므로 복원 로직을 넣지 않기로 결정.
`_looks_garbled()`가 이를 감지해 경고 로그를 남기며, 이 경우 제목/초록 직접
입력이 확실한 경로다.

### 16.2 데모 웹 서버 (`demo/`) — **삭제됨 (역할 종료)**

팀 시연용 임시 화면이었다. `paper_assistant.analyze()` 하나만 호출해 백엔드 통합
계약을 그대로 시연했고, "분자 특성 예측 GNN" 쿼리로 유사 논문 20편·리뷰 패턴
(baselines 19/20 등)·게재 경향이 정상 렌더되는 것까지 브라우저로 확인했다 —
계약이 실제로 동작함을 입증한 뒤 소임을 다했다.

실제 프론트([AICE-FE](https://github.com/AICE-GACHON/AICE-FE), Vite + React) 연동이
끝나면서 예정대로 폴더째 삭제했다. 계약을 화면에 옮긴 참고 구현이 필요하면 이제
AICE-FE의 `src/workspace/report/`를 보면 된다.

---

## 17. LLM 경로(토글 ON) 실측 평가 (2026-07-26)

LSTM 원논문 초록을 use_llm=True로 실행($0.05) — Haiku 태깅 + Sonnet 종합의 첫 실전.

**태깅(Haiku) — 정확**: MC-LSTM("mass conservation으로 LSTM 확장"), xLSTM("원 LSTM
계승"), S4(장기의존성은 같지만 LSTM 인용 아님 → citation 태그 제외)까지 미묘한 구분을
정확히 함. **주의**: `citation` 태그는 실제 참고문헌이 아니라 주제 기반 추론이라
단정은 근거가 약할 수 있음.

**종합(Sonnet) — 사실 충실 + 인사이트**: "MC-LSTM·Boosted LSTM은 reject → 단순 구조
변형만으론 통과 어렵다"는 데이터 근거 관찰 도출. 언급 논문·decision·수치가 전부 실제
결과와 일치(환각 없음).

이 평가에서 게재 경향 표본 문제를 발견 → §14.6으로 수정.

---

## 18. 리뷰 패턴 재설계 — 빈도 → lift + 당락 대조 (2026-07-26)

### 18.1 문제: 빈도 정렬은 코퍼스 상수를 1등으로 올린다

`aggregate_by_aspect`가 `paper_count` 내림차순으로 정렬하니, base rate가 높은
aspect가 쿼리와 무관하게 항상 상위를 차지했다. 실측 base rate(43,033편 기준):

| aspect | 지적받은 논문 | base rate |
|---|---|---|
| baselines | 33,896 | **78.8%** |
| clarity | 28,319 | 65.8% |
| significance | 27,368 | 63.6% |
| theoretical_soundness | 26,660 | 62.0% |
| novelty | 17,824 | 41.4% |
| related_work | 16,688 | 38.8% |
| experimental_scale | 11,273 | 26.2% |
| reproducibility | 9,691 | 22.5% |

"분자 특성 예측 GNN" 쿼리의 이전 출력은 baselines 17/20 → clarity 13/20 →
significance 13/20 순이었는데, lift로 환산하면 각각 **1.08 / 0.99 / 1.02**다.
즉 상위 3개가 전부 "ML 논문이면 다 받는 지적"이었다. 사용자에게 정보량 0.

### 18.2 해결 1 — lift + 이항검정

`aspect_base_rates` 테이블(scripts/build_base_rates.py로 사전 계산)을 분모로 삼아
`lift = 관측률 / base_rate`를 계산하고, 이웃 n=20에서의 우연을 걸러내기 위해
이항검정 단측 p값을 함께 낸다. `is_distinctive = lift ≥ 1.25 and p ≤ 0.05`.

정렬 키는 `(is_distinctive, lift, paper_count)`. base_rates가 없으면 lift가 전부
None이 되어 자연히 기존 빈도순으로 폴백한다(DB 없는 테스트 경로).

**중요한 부수효과**: 위 GNN 쿼리는 이제 "이 주제 특유의 지적 없음"을 반환한다.
이게 정직한 답이다 — 억지 인사이트를 만들지 않는 것 자체가 품질이다.

### 18.3 해결 2 — 당락 대조 (실질적으로 가장 유용한 정보)

같은 이웃 안에서 **이 지적을 받은 논문 vs 받지 않은 논문의 accept율**을 비교한다.
"이 지적은 받아도 붙는다 / 받으면 떨어진다"가 사용자가 실제로 쓰는 정보다.

표본이 작아서(이웃 20편 중 4편이 지적받는 식) 검정이 필수다. 기대빈도 5 미만이라
카이제곱을 못 쓰므로 **단측 Fisher 정확검정**을 쓴다(`fisher_exact_less`).
scipy 없이 `math.comb`로 직접 계산 — n이 작아 정확 계산이 충분히 빠르다.

GNN 쿼리 실측:

| 지적 | 지적받음 | 미지적 | Fisher p | 유의 |
|---|---|---|---|---|
| 신규성 부족 | 2/11 (18%) | 6/9 (67%) | 0.040 | ✔ |
| 중요성·동기 | 3/13 (23%) | 5/7 (71%) | 0.052 | — |
| 재현성 | 0/4 (0%) | 8/16 (50%) | 0.102 | — |

재현성은 "0% 통과"라 가장 극적으로 보이지만 n=4라 유의하지 않다. 검정 없이 이걸
결론으로 냈으면 노이즈를 사실로 파는 셈이었다. `is_contrast_significant`가
False면 UI·요약 모두 단정하지 않는다.

**decision 처리**: `unknown`은 분모에서 제외, `withdrawn`은 '통과 못함'으로 센다
(리뷰가 나쁘게 나온 뒤 철회한 경우가 대부분이라 reject와 같은 신호).
지적항목이 하나도 없는 이웃도 대조군 분모에 들어가야 하므로 `all_paper_ids`를
별도로 넘긴다 — points에 등장한 논문만 쓰면 대조군이 비어버린다.

### 18.4 Sonnet 종합 프롬프트 보강

구조화 사실에 lift·base_rate·contrast_significant를 함께 실었다. 빈도만 주면
모델이 "베이스라인 비교를 강화하세요" 같은 코퍼스 상수를 결론처럼 써버린다.
프롬프트에 "lift가 1.0 근처면 ML 논문 전반의 규범이므로 이 주제의 발견처럼
제시하지 말 것", "contrast_significant가 false면 단정하지 말 것", "두드러진 게
없으면 없다고 말할 것"을 명시했다.

### 18.5 남은 문제 (다음 순위)

- `examples`에 강점 문장이 섞인다. needs_llm_split venue(62,346건)는 리뷰 본문
  전체가 `weaknesses`에 들어와 HeuristicExtractor가 강/약을 못 가린다.
  → 리뷰 추출 품질 개선(2순위 rating 노출과 함께 검토).
- `similarity_percentile`이 top-K에서 전부 100으로 포화 — 참조 분포가 무작위쌍이라
  구조적으로 변별 불가. 폐기 또는 재정의 필요(3순위).

---

## 19. 리뷰 점수(rating) 노출 + 표본 편향 보정 (2026-07-26)

### 19.1 안 쓰던 최강 신호

`reviews.rating`은 168,217건 **100% 커버리지**로 있으면서 Report에 한 번도 나가지
않았다. 당락을 가장 잘 가르는 단일 신호다 — 코퍼스 accept 평균 6.24 vs reject 4.71.

### 19.2 원점수를 그대로 주면 안 되는 이유

venue별 실측(scripts/build_venue_stats.py):

| venue | 논문 | accept율 | 척도 | 평균 | accept평균 | reject평균 | 당락경계 |
|---|---|---|---|---|---|---|---|
| ICLR 2020 | 2,213 | 31.0% | 1~8 | 4.42 | 6.24 | 3.60 | 5.5 |
| ICLR 2022 | 2,617 | 41.8% | 1~10 | 5.52 | 6.63 | 4.73 | 6.0 |
| ICLR 2025 | 11,520 | 32.1% | 1~10 | 5.15 | 6.46 | 4.80 | 6.0 |
| NeurIPS 2021 | 2,768 | **95.1%** | 1~10 | 6.31 | 6.37 | 5.24 | — |
| NeurIPS 2024 | 4,236 | **95.2%** | 1~10 | 5.87 | 5.92 | 4.93 | — |

**ICLR 2020은 1~8 척도**라 6.0의 의미가 다르다. venue별 평균도 1점 가까이 벌어진다
(ICLR 2025 5.15 vs NeurIPS 2021 6.31). 그래서 `venue_stats`를 기준선으로 두고
**항상 상대값으로만** 말한다 — `rating_vs_venue`, `rating_vs_threshold`.

### 19.3 당락 경계는 실제로 존재한다

ICLR 2025의 평균 rating 0.5 단위 버킷별 통과율:

| 평균 rating | 5.0 | 5.5 | **6.0** | 6.5 | 7.0 |
|---|---|---|---|---|---|
| n | 1,462 | 1,007 | 2,044 | 887 | 758 |
| 통과율 | 8% | 20% | **66%** | 94% | 95% |

5.5→6.0 사이에서 급격히 갈린다. `threshold_50`은 통과율이 50%를 처음 넘는 버킷으로
정의한다. ICLR은 2022~2025 내내 6.0으로 안정적이다.

### 19.4 ⚠️ 표본 편향 — 이번 작업에서 발견한 정확성 버그

**NeurIPS는 코퍼스의 95%가 accept다.** OpenReview가 NeurIPS는 채택 논문 위주로만
공개하기 때문이고, 실제 채택률(~25%)이 아니다. 이 편향은 기존 출력을 명백히
틀리게 만들고 있었다:

- `venue_trends`가 "NeurIPS 4/5 accept (80%)"를 그대로 노출 → 사용자는 "NeurIPS가
  붙기 쉽다"고 읽는다. 정반대다.
- 당락 경계 추정도 reject 표본이 4.7%뿐이라 무의미해진다.
- 재투고 흐름 "ICLR reject → NeurIPS accept"도 같은 편향의 산물이다
  (NeurIPS에서 reject된 재투고는 애초에 데이터에 없다).

**대응**: `is_coverage_biased`(reject 비중 < 15%)를 세우고,
- 당락 경계는 **추정하지 않는다**(None).
- `venue_trends`에 `corpus_accept_rate` / `accept_lift`를 함께 실어 절대값 대신
  **그 학회 자신의 코퍼스 대비**로 말하게 한다. 이러면 "NeurIPS 80%"가
  "코퍼스 95% 대비 **0.84배** = 오히려 평균 이하"로 정정된다.
- UI와 요약 프롬프트 양쪽에 경고를 박는다.

### 19.5 출력에 추가된 것

- `SimilarPaper`: `avg_rating`, `rating_count`, `rating_spread`,
  `rating_vs_venue`, `rating_vs_threshold`
- `RatingContext`(신규): 이웃 평균 / 통과·탈락 평균 / 당락 경계 / **리뷰어 의견이
  갈린 논문**(spread ≥ 4.0) / 편향 venue 목록
- `VenueTrend`: `corpus_accept_rate`, `accept_lift`, `is_coverage_biased`

GNN 쿼리 실측: 이웃 평균 5.37, 통과 6.25 vs 탈락 5.19, 경계 ICLR 2025 기준 6.0.
"평균 5.37이면 경계 6.0에 못 미친다"가 사용자가 받는 실질 정보다.

`rating_spread`(최고−최저)는 리뷰어 합의 여부를 드러낸다. 6점 차로 갈린 논문은
"주제 자체가 평가가 엇갈린다"는 신호라 재투고 판단에 쓸 수 있다.

### 19.6 남은 문제

- 재투고 흐름도 §19.4 편향의 영향을 받는다. 현재는 경고만 있고 보정은 없다.
- `similarity_percentile`은 여전히 top-K에서 100 포화 (3순위).

---

## 21. 논문별 유사도 점수 폐기 + 검색 신뢰도 도입 (2026-07-26)

### 20.1 문제: 점수가 전부 100이었다

`similarity_percentile`은 무작위 논문쌍을 참조 분포로 쓴다(§11.2). 문제는 검색
결과가 그 분포의 극단 꼬리에만 있다는 것이다. top-200 코사인 실측:

| 순위 | 1 | 5 | 10 | 20 | 50 | 100 | 200 |
|---|---|---|---|---|---|---|---|
| GNN 쿼리 | 0.9510 | 0.9442 | 0.9411 | 0.9378 | 0.9317 | 0.9231 | 0.9149 |
| LSTM 쿼리 | 0.9546 | 0.9496 | 0.9459 | 0.9410 | 0.9343 | 0.9267 | 0.9198 |

**top-20의 코사인 폭이 0.013**이다. 백분위로 바꾸면 1위와 20위가 1%p 안쪽으로
붙는다(99.6~100.0). 실제 출력은 20편 전부 "유사도 상위 1%"였다.
어떤 단조 변환을 써도 이 구간을 못 편다 — **논문별 유사도 점수는 원리적으로
불가능**하므로 폐기한다.

부수 버그도 있었다: FTS로만 걸린 논문은 코사인이 없어 `None`인데
`p.similarity_percentile or 0.0`이 **0.0**으로 바꿔 내보냈다. "무작위 논문보다도
안 닮았다"는 뜻이 되는데, 실제로는 "벡터 검색 후보에 없었다"일 뿐이다.

### 20.2 대체 1 — 쿼리 단위 신뢰도 (진짜 신호가 여기 있다)

논문 사이는 못 가르지만 **쿼리 사이는 아주 잘 갈린다**. top-5 평균 코사인:

| 도메인 안 | | 도메인 밖 | |
|---|---|---|---|
| LoRA | 0.9664 | 바흐 푸가 대위법 | 0.8668 |
| Federated learning + DP | 0.9649 | 한자동맹 무역로 | 0.8599 |
| Diffusion models | 0.9538 | 치즈 숙성 미생물학 | 0.8586 |
| Transformer | 0.9478 | 무릎 인공관절 수술 | 0.8568 |
| LSTM | 0.9512 | 안데스 조산운동 | 0.8522 |
| GNN(분자) | 0.9457 | | |

**겹치는 구간이 전혀 없다** (0.8668 vs 0.9457). 경계는 무작위쌍 분포의 분위수에
맞춰 잡는다 — strong ≥ 0.93(99분위 초과), moderate ≥ 0.90(95분위), 그 아래 weak.

이 판정이 없을 때의 실패 모드가 심각했다: "한자동맹 무역로"를 넣으면
"Association Rules in QUBO Samples" 같은 논문 20편을 아무 경고 없이 내놓고,
그 위에 리뷰 패턴·당락 대조·점수 분석까지 정상적으로 붙여줬다. 전부 노이즈인데
형식은 완벽해서 더 위험하다.

weak 판정이면 `is_reliable=False`, 요약 **첫 줄**에 경고, 데모는 빨간 배너.

### 20.3 대체 2 — `match_type` (왜 걸렸는지, $0)

점수 대신 검색기 두 개의 히트 여부를 근거로 준다. 이미 있는 데이터라 비용 0.

- `both` — 임베딩과 용어 양쪽. 가장 믿을 만한 매칭
- `semantic` — 임베딩만. 접근은 비슷한데 쓰는 용어가 다르다
- `lexical` — 용어만. 같은 단어를 쓰지만 접근은 다를 수 있다

### 20.4 부수 수정 — HNSW `ef_search`

`CANDIDATE_POOL = 50`인데 pgvector의 **기본 `hnsw.ef_search`는 40**이다.
`LIMIT 200`을 걸어도 40행만 돌아오는 것을 실측으로 확인했다. 벡터 후보가 40개로
잘려 RRF 결합이 한쪽만 얕아지고 있었다.

`set_config('hnsw.ef_search', ..., true)`로 트랜잭션 로컬 설정을 건다
(`SET`은 바인드 파라미터를 못 받고, 세션 설정은 풀 반납 후에도 남는다).
수정 후 같은 쿼리의 게재 경향이 ICLR 4/15 → 5/15로 바뀌었다 — 실제로 영향이 있었다.

### 20.5 계약 변경

`SimilarPaper.similarity_percentile` **제거**, `match_type` 추가.
`Report.confidence`(`RetrievalConfidence`) 추가. `SearchResult`는
`similarity_percentile` → `cosine`(원시값, 신뢰도 판정 전용) + `match_type`.

`similarity_percentile()` 함수 자체는 남는다 — 쿼리 단위 판정의 기준선 근거이고,
경계값(0.90/0.93)이 이 분포에서 나왔기 때문이다. 다만 **논문별 표시 금지**를
독스트링에 명시했다.

---

## 20. arXiv ID 매칭 + Semantic Scholar 보강 (2026-07-26)

> **이 절은 설계·구현만 다룬다. 실제로 돌린 결과는 §25(2026-08-02)에 있다.**
> 아래 "전부 NULL"은 **구현 시점의 과거 상태**이고, 지금은 채워져 있다.

§2.3의 `fetch_s2` / `fetch_arxiv` 단계가 그동안 비어 있었다 — `S2_API_KEY`는 config가
읽기만 하고 아무도 쓰지 않았고, `papers.arxiv_id`/`s2_paper_id`는 43,515편 전부 NULL,
`citations`는 0행이었다. 그 결과 재투고 매칭의 1순위(arXiv ID 일치, confidence 1.00)가
구조상 no-op이었다(§15.1). 이 절은 그 구멍을 메운 구현이다.

### 20.1 왜 "제목으로 S2에 물어보기"가 아닌가 (실측)

| 엔드포인트 | 실측 결과 | 결론 |
|---|---|---|
| 키 없는 모든 호출 | **항상 429** (익명 풀이 전역 포화) | API 키 필수 |
| `GET /paper/{id}` (키 O) | 200, 정상 | 단건 확인용 |
| `POST /paper/batch` | 1요청 **500 id**, 미발견은 `null`, 순서 보존 | **주력** |
| `GET /paper/search/match` | 제목 1건 = 요청 1건, 키가 있어도 429 잦음 | 43k 매칭에 사용 불가 |
| `GET /paper/search/bulk` | 1페이지 1,000건 + token, `query` 없이 venue/year만으로 동작. 다만 **깊은 페이지에서 429/500이 연속**으로 나고 몇 분씩 막힌다 | 보조 수단 |

즉 **제목→논문 매칭을 S2로 43,515번 하는 경로는 존재하지 않는다.** 그래서 제목 매칭은
arXiv 쪽에서 대량으로 받아 로컬에서 하고, S2는 arXiv ID를 키로 batch 조회한다.

### 20.2 arXiv OAI-PMH 하베스트 (`ingest/arxiv_client.py`)

1요청에 **1,300건 / 약 16초**(≈87건/초). cs+stat, datestamp 2018-01-01 이후를 통째로
받아 `data/raw/arxiv_meta.jsonl.gz`에 (id, title, created, updated, categories,
저자 keyname)만 남긴다. 실측 특성:

- `from`은 제출일이 아니라 **datestamp(최종 수정일)** 기준 → 오래된 논문도 수정 이력이
  있으면 들어온다. 커버리지엔 유리하고 누락 위험은 없다.
- resumptionToken에 `from=...&skip=N`이 들어 있어 **토큰만 저장하면 중단 후 이어받기**가
  된다 (`data/raw/arxiv_harvest.json`).
- 과부하 시 503 + `Retry-After`. 병렬 하베스트는 금지되어 순차로만 받는다.
- `status="deleted"` 레코드는 metadata가 없다 → 건너뛴다.
- 제목이 XML에서 줄바꿈되어 오므로 공백을 flatten해야 정규화 제목이 맞는다.

### 20.3 매칭 규칙 (`ingest/arxiv_matcher.py`)

오탐 하나가 재투고 링크를 confidence 1.00으로 오염시키므로 **보수적으로** 잡았다.

1. 정규화 제목(소문자+영숫자, `submission_linker.normalize_title` 재사용) **완전 일치**만 후보
2. 정규화 제목이 25자 미만이면 건너뜀 (일반 명사구 충돌)
3. 양쪽에 저자가 있으면 **성(姓)이 최소 1개 겹쳐야** 함 — 동명 논문이 실제로 존재
4. 후보가 여럿이면 성 겹침이 가장 큰 것, **동점이면 포기**
5. arXiv 등록 연도가 투고 연도보다 1년 이상 늦으면 다른 논문으로 간주

### 20.4 S2 보강 (`ingest/s2_client.py`, `ingest/s2_enricher.py`)

- **by-arxiv** (주력): `ARXIV:<id>`로 500건씩 batch 조회 → `s2_paper_id`,
  `citation_count`(신규 컬럼), `final_venue`(프리프린트 서버는 제외), `authors.s2_author_id`.
  저자 ID는 성으로 맞추고 **논문 안에 같은 성이 둘 이상이면 건너뛴다**.
- **by-venue** (보조): 아직 s2_paper_id가 없는 논문을 학회 단위로 bulk 조회해 제목으로
  맞춘다. S2에는 채택 논문만 학회 venue로 색인되므로 reject/withdrawn은 거의 못 잡는다.
  실측 커버리지는 낮다 — ICLR 2020 채택 687편 중 S2 `venue=ICLR&year=2020` 슬라이스에
  없는 것이 402편이고, 반대로 그 슬라이스에는 **ICLR 2021 논문(DDIM)이 섞여** 있다.
  S2의 publication year가 학회 연도와 어긋나기 때문 → 연도 ±1로 넓히고 학회 단위 제목
  맵으로 맞춘다. 그리고 페이지 단위로 즉시 반영해 중간에 막혀도 성과가 남게 했고,
  연속 3회 실패하면 by-venue를 중단한다(재시도 시간 낭비 방지, 재실행하면 이어짐).
- **citations**: batch의 `references.paperId`로 **코퍼스 내부 엣지만** 적재.

### 20.5 실행

```bash
python scripts/run_enrichment.py      # 하베스트 → arXiv 매칭 → S2 보강 → 재투고 재계산
```

⚠️ **`init_db.sql`에 컬럼을 넣는 것만으로는 이미 떠 있는 DB가 따라오지 않는다.**
예전에는 `scripts/migrate_s2_fields.sql`을 먼저 돌렸고, 같은 내용이 `init_db.sql`에
들어간 뒤 §22에서 삭제했다 — 그런데 `init_db.sql`은 **컨테이너 최초 기동 때 한 번만**
실행되므로, 그 시점에 이미 살아 있던 DB에는 `citation_count`가 끝내 생기지 않았다.
실제로 §25에서 이 문제로 S2 보강이 첫 UPDATE에서 죽었고, alembic `0008`로 복구했다.
**코퍼스 테이블에 컬럼을 추가할 때는 `init_db.sql`과 마이그레이션을 항상 같이 쓸 것.**

하베스트가 전체 시간의 대부분(2~3시간)이고 나머지는 10~20분. 모든 단계가 멱등이다.


## 22. 통합 후 정리 (2026-07-26)

백엔드와 합친 뒤, 설계 단계에서 만들어져 더는 쓰이지 않는 것들을 걷어냈다.
근거는 전부 위 절들에 남아 있으므로 여기서는 **무엇을 왜 지웠는지**만 적는다.
지운 파일은 git 히스토리(main)에 그대로 있다.

### 22.1 죽은 코드

- **`similarity_percentile()`** (§11.2, §21) — §21에서 Report에서 빼기로 한 뒤로
  호출부가 하나도 없었다. docstring은 "`retrieval_confidence()`가 쓴다"고 적혀 있었지만
  실제로는 원시 코사인을 임계값과 직접 비교한다. 참조 분위수 표와 테스트 6개를 함께 삭제.
- **`_greedy_cluster()`** (§14) — 임베딩 클러스터링을 폐기하면서 "범용 유틸로 남겨둔다"고
  했던 함수. 1년이 지나도 쓰일 곳이 없었고 테스트 5개가 죽은 코드를 지키고 있었다.
- **탐색용 스크립트 10개** (`verify_*`, `measure_similarity_dist`, `probe_venues`,
  `count_all`, `eval_llm_query`, `load_pilot`, `ingest/run_pilot`) — 전부 외부 API를
  때려 결과를 print하고 끝나는, 설계 근거를 만들기 위한 일회성 도구였다. 그 근거는 이
  문서에 수치로 박혀 있으므로 스크립트 자체는 유지할 이유가 없다. `scripts/`에는 DB에
  쓰는 운영 배치만 남겼다.
- **`demo/requirements.txt`** — fastapi·uvicorn·python-multipart 세 줄뿐이었고 셋 다
  백엔드가 생기면서 루트 `requirements.txt`에 들어갔다.

`demo/` 자체는 한동안 **남겼다.** 한 번 지웠다가 되살린 적이 있다 — 백엔드가 같은
계약을 구현했으니 중복이라고 판단했지만, 분석 결과를 **눈으로 볼 수 있는 화면이
이것뿐**이라는 점을 놓쳤다. Swagger는 JSON을 보여줄 뿐 §4의 표시 규칙(신뢰도 경고,
유사도 % 금지, lift 기준 강조)이 화면에서 어떻게 보여야 하는지 알려주지 못한다.
예고한 대로 **프론트(AICE-FE) 연동이 끝난 시점에 삭제했다** (§16.2).

### 22.2 실제로 위험했던 것

- **파이프라인 빌드에 락이 없었다.** 백엔드는 `analyze()`를 `BackgroundTasks`(스레드풀)에서
  부른다. 첫 요청 두 건이 겹치면 두 스레드가 동시에 SPECTER2를 로드했다. `threading.Lock`을
  걸고, embedder를 `use_llm`과 분리해 캐시했다 (예전에는 토글만 바꿔도 모델을 다시 로드했다).
- **통계 캐시가 실패를 영구히 캐시했다.** `load_base_rates()`/`load_venue_stats()`는 조회에
  실패하면 `{}`를 캐시에 넣었고, 서버처럼 오래 뜨는 프로세스에서는 DB가 잠깐 흔들린 뒤
  재시작 전까지 lift와 rating 맥락이 영영 사라졌다. 이제 성공했을 때만 캐시한다.
  두 로더는 성격이 같아 `db/stats.py` 하나로 합쳤다.

### 22.3 구조

- `detail.py` / `revisions.py` → **`query/`** (조회 전용, 임베딩·LLM 없음)
- `graph/base_rates.py` + `graph/venue_stats.py` → **`db/stats.py`** (둘 다 DB 조회 캐시라
  graph 노드가 아니다)
- 세 API 클라이언트의 재시도 루프 → **`ingest/_http.py`**. 재시도 *조건*은 API마다
  다르므로(arXiv는 503+Retry-After, S2는 429+5xx, OpenReview는 429+401 재인증) 조건은
  각자 `decide`로 두고, 요청→대기→재시도→소진 예외만 공유한다.
- 환경설정 단일화: `paper_assistant/config.py`가 공유 값(DB·LLM 토글)의 소스이고
  `app/core/config.py`는 그것을 읽는다. 예전에는 `PAPER_ASSISTANT_USE_LLM`을 세 곳
  (`app.core.config`, `paper_assistant.config`, `graph/llm.py`의 `os.getenv`)에서 각각
  읽었다.
- `aspect_base_rates` / `venue_stats`의 `CREATE TABLE`이 `init_db.sql`과 배치 스크립트
  양쪽에 있었다. 스키마 소유자는 `init_db.sql` 하나로 정리.


## 23. 근거 추적 도입 — 검색된 원문을 생성 컨텍스트로 (2026-07-26)

### 23.1 문제

"이거 RAG 맞나?"라는 질문에서 출발했다. 코드를 보면 답은 **아니오에 가까웠다.**

`USE_LLM=0`(기본)에서는 생성 자체가 없다. 켜도 Sonnet에 들어가는 건 lift·p값·
accept율 같은 **집계 숫자**뿐이고, 리뷰 본문 96만 건은 단 한 글자도 가지 않았다.
`aggregate_by_aspect`가 aspect마다 대표 문장 3개를 뽑아 `ReviewPattern.examples`에
담고 있었는데, **프론트에는 내려가면서 프롬프트에는 안 들어갔다.**

### 23.2 한 것

1. **`examples`를 프롬프트에 넣었다** (`facts["evidence"]`).
   검색된 리뷰 문장이 생성 컨텍스트에 실제로 들어가므로 정의상 RAG가 성립한다.
2. **메타리뷰를 파이프라인에 편입했다.** `papers.meta_review`는 그동안 조회 API로만
   나가고 분석에는 안 쓰였다. AC의 최종 판단이라 개별 리뷰보다 신호가 강하고
   논문당 1건뿐이라 컨텍스트에 넣기도 좋다. `hybrid_search`가 같이 읽어 온다.
3. **인용을 검증한다.** `graph/evidence.py`의 `validate_citations()`가 모델이 쓴
   `[E1]`을 근거 풀과 대조해 없는 라벨을 지운다. 이게 없으면 "인용해 달라"는
   부탁일 뿐이고, `[E9]`를 지어내도 화면에는 근거가 달린 것처럼 보인다.
4. **`examples`에 출처를 실었다.** `list[str]` → `list[ReviewExample]`
   (`review_point_id` 포함). 인용을 `review_points.id`까지 되짚을 수 있어야
   근거 추적이 성립한다.

통계는 그대로 뒀다. 원문만 넣고 lift/Fisher를 빼면 §18에서 실측한 문제
(코퍼스 상수를 발견처럼 쓰는 것)가 그대로 재발한다. **원문을 추가하되 통계를
가드레일로 남기는 것**이 핵심이다.

### 23.3 대표 문장 선정을 고쳐야 했다 (실측)

붙이자마자 인용 품질이 드러났다. GNN 쿼리로 뽑은 근거 10건 중 절반이
지적이 아니었다.

```
[E3] experimental_scale — "It is based on strong and reliable previous results,
                           which makes it a robust model."        ← 칭찬
[E6] baselines          — "**Summary** This paper proposes ForceNet …"  ← 요약
[E7] significance       — "The paper propose a neural network force field …" ← 요약
```

원인은 미분리 리뷰(§11, 코퍼스의 37%)다. 본문 전체가 weakness로 라벨링돼 있는데
대표 문장을 **길이순**으로 골랐더니 요약 단락이 1등으로 올라왔다. 통계에서는
감수했던 한계지만(분모 정의를 base rate와 맞춰야 한다), 원문을 인용하기 시작하면
그대로 노출된다.

수정: 대표 문장 정렬 키를 `(from_unsplit, -len)`으로 바꿔 **분리 포맷 리뷰를
먼저** 쓴다. 집계 수치는 건드리지 않는다. 같은 쿼리에서 미분리 출처가
10건 중 **0건**이 됐고, 뽑힌 문장은 전부 실제 지적이었다.

    [E5] baselines  — "My primary concern stems from the experimental setup's
                       consistency. The baseline methods used for comparison…"
    [E9] clarity    — "Section 5 (method) is too condensed to present a clear
                       picture of how the proposed…"

다른 후보가 없어 미분리 문장이 뽑히면 `from_unsplit_review=true`로 표시해 내려간다.

### 23.4 부수 효과 — DB가 죽었을 때 30초씩 멈추던 문제

근거 테스트를 붙이면서 노드 단위 테스트가 191초로 늘었다. 원인은 커넥션 풀이었다.
`ConnectionPool`의 기본 대기가 30초라, 컨테이너가 내려간 상태에서
`load_venue_stats()` 한 번에 30초를 버렸다. 운영에서도 DB가 흔들리면 분석 요청
하나가 그만큼 멈춘다는 뜻이다.

풀에 `timeout=5`, `connect_timeout=5`를 줘서 빨리 포기하게 했고, 노드 테스트는
통계 로더를 스텁으로 바꿔 DB를 아예 타지 않게 했다(191초 → 0.7초). 통계 유무에
따라 검증 대상이 달라지던 비결정성도 같이 없앴다.

### 23.5 검증의 한계 — 실측으로 드러난 것

토글을 켜고 실제 Sonnet 출력을 받아보니 인용 검증이 잡지 못하는 실패 모드가 있었다.
요약이 이렇게 썼다.

> "실제로 GemNet 계열 등 우수 사례는 명확한 동기 제시가 **강점**으로 꼽힙니다[E4]."

`E4`는 **다른 논문**(On the Scalability of GNNs)의 **실험 규모 비판**이다
(`review_points.id=1185158`, "pre-training data consists of only 5 million
molecules ... insufficient"). 라벨이 실재하므로 `validate_citations()`를 통과했지만,
문장은 그 원문이 말하지 않는 내용이다.

즉 이 검증이 보장하는 것은 **"인용이 실제 원문으로 역추적된다"**까지이고,
**"그 원문이 그 주장을 뒷받침한다"**는 아니다. 지어낸 라벨은 막지만, 실재하는
라벨을 엉뚱한 문장에 붙이는 것은 막지 못한다.

덧붙여 근거 풀에는 strength 항목이 **하나도 없다** — 추출기가 weakness와 question만
만들기 때문이다(§11). 따라서 요약의 모든 '강점' 주장은 구조적으로 근거가 없다.

완화책으로 프롬프트에 두 줄을 넣었다: (1) 인용은 그 항목의 원문이 그 항목의 논문에
대해 말하는 내용만 뒷받침한다, (2) 근거 풀에 강점은 없으니 강점 주장에 인용을 붙이지
말라. 근본 해결은 인용 문장과 원문을 대조하는 **별도 검증 단계**이고, 그건 LLM 호출이
한 번 더 들어간다.

프론트는 그때까지 인용을 '검증된 사실'로 표시하지 말고 원문을 함께 펼쳐
사용자가 대조하게 해야 한다.

### 23.6 남은 것

검색 단위는 여전히 **논문**이다(title+abstract 벡터 1개). `review_points.embedding
vector(768)` 컬럼이 있지만 값을 쓰는 코드 경로가 아예 없어 전부 NULL이다.
채우면 "쿼리 초록과 가장 가까운 지적 문장"을 논문 20편이라는 틀 밖에서 직접
검색할 수 있다 — 진짜 2단계 RAG.

⚠️ SPECTER2로 채우면 안 된다. §14 실측대로 짧은 리뷰 문장 유사도가 0.872에
압축돼 변별이 안 된다. 문장용 모델(`bge-small`, `all-MiniLM` 등)이 따로 필요하고
차원이 달라 컬럼 정의도 바꿔야 한다. weakness 96만 건 CPU 임베딩에 몇 시간.

### 23.7 이걸 RAG라고 부를 수 있는가

**부를 수 있다.** 다만 흔한 RAG와 순서가 달라서, 대외적으로 설명할 때 정확히
말하려면 무엇이 생성 컨텍스트에 들어가는지를 구분해야 한다.

| 단계 | LLM에 들어가는 것 | 성격 |
|---|---|---|
| `similarity_tagging` | 검색된 논문의 **제목+초록 원문** | 교과서적 RAG |
| `synthesis` | 검색된 **리뷰 지적 문장 10건 + AC 메타리뷰 3건**(원문)<br>+ lift·Fisher 검정 등 집계 통계 | RAG + 통계 가드레일 |

`USE_LLM=0`(기본)이면 생성 자체가 없으므로 **그때는 RAG가 아니다** — 검색 + 통계 +
템플릿이다. 다만 근거 풀은 LLM을 꺼도 채워진다(검색 결과이지 생성 결과가 아니므로).
그래서 $0 모드에서도 "이 결론의 출처" 목록은 항상 볼 수 있다.

순진한 RAG와 다른 점은 §18에서 실측한 그대로다 — 리뷰를 그냥 넣고 요약시키면
코퍼스 상수(78.8%가 받는 지적)를 발견처럼 쓰고, 표본 20편의 우연을 결론으로 낸다.
그래서 **통계로 먼저 걸러낸 뒤 원문을 함께 넘긴다.**

발표에서는 이렇게 말하면 정확하다:

> 검색은 RAG와 동일한 하이브리드 방식(임베딩 + 전문검색 RRF)을 쓰되, 생성 단계에는
> 원문과 **통계 검증 결과를 함께** 넘긴다. LLM은 결론을 만들지 않고 문장으로 옮기며,
> 생성된 모든 인용은 `review_points.id`로 역추적된다.

단, §23.5의 한계(라벨은 실재하지만 의미 일치는 미검증)를 함께 말하지 않으면
과장이 된다.


## 24. held-out 평가 도입 — 그리고 그것이 말한 것 (2026-07-29)

`scripts/eval_retrieval.py`. 코퍼스 논문 P를 검색에서 제외하고 P의 제목+초록을
쿼리로 넣어, 파이프라인이 내놓은 예상 지적을 **P가 실제로 받은 지적**과 대조한다.
사람 라벨링이 필요 없다 — 43,515편이 전부 정답이 달린 시험 문제다.

누출 차단: 정답 논문 자신, 재투고 링크로 이어진 논문, 제목이 같은 논문을
검색 결과에서 뺀다. 정답지 품질을 위해 **모든 리뷰가 분리 포맷인 논문만** 평가
대상으로 삼는다(미분리는 요약·칭찬까지 지적으로 세어 aspect가 부풀기 때문).

### 24.1 첫 실행에서 프로덕션 버그가 나왔다

`_fulltext_search`가 `to_tsvector`의 lexeme을 그대로 `to_tsquery`에 넘기는데,
URL이 든 초록은 `github.com/a/b](https://…).` 같은 lexeme을 만든다. 괄호·콜론
때문에 tsquery 파서가 SyntaxError를 냈다 — 즉 **초록에 URL이 있으면 분석이
통째로 실패했다.** `quote_literal(lexeme)`로 고쳤고, 매칭 33,638편·top-50·1위가
인용 전후 동일함을 확인했다(동작 불변). 회귀 테스트 2개 추가.

### 24.2 파이프라인이 '검색 없음' 베이스라인에 진다

베이스라인은 **검색을 아예 하지 않고** 코퍼스에서 가장 흔한 aspect 3개
(baselines·clarity·significance)를 모든 논문에 대해 찍는다.

| 시드 / 표본 | 베이스라인 F1 | 무작위이웃 F1 | 모델 F1 | 모델/베이스 | 모델/무작위 |
|---|---|---|---|---|---|
| 0 / 100편 | 0.605 | 0.369 | 0.397 | **0.657** | 1.076 |
| 1 / 200편 | 0.580 | 0.391 | 0.453 | **0.781** | 1.157 |

두 독립 표본에서 같은 그림이다. 그리고 §18의 목표(두드러진 지적 골라내기)로
공정하게 재려고 **흔한 aspect를 제외하고** 드문 것만 놓고 겨뤄도 결과는
**0.769배**로 여전히 진다(n=100→유효 76, @2).

### 24.3 더 불편한 숫자 — 검색이 무작위보다 8~16%밖에 낫지 않다

대조군으로 **무작위 논문 20편**을 이웃으로 삼아 같은 집계를 돌렸다.
모델/무작위 = **1.076~1.157배**.

즉 aspect 예측이라는 과제에서 **"의미적으로 비슷한 논문은 비슷한 지적을 받는다"는
전제가 거의 성립하지 않는다.** 그럴듯한 이유가 있다 — 리뷰 지적은 논문의 *주제*보다
*실행 품질*(실험을 충분히 했나, 글이 명확한가)에 좌우되고, 그건 초록에 드러나지
않는다. GNN 논문 둘이 완전히 다른 지적을 받을 수 있다.

### 24.4 창문 확대는 실제로 도움이 된다

top_k 20 → 100에서 모델 F1 0.397 → **0.431** (모델/베이스 0.657 → 0.712).
n=20에서 lift는 분산이 크다 — base rate 0.225인 aspect가 6/20이면 lift 1.33,
5/20이면 1.11로 논문 한 편에 순위가 뒤집힌다. 이웃을 늘리면 안정화된다.
다만 격차를 메우지는 못한다.

### 24.5 해석과 한계

**해석**: aspect *예측*은 우리 데이터로 잘 되지 않는다. 반면 §23의 질적 프로브는
검색이 **구체적인 지적 문장**은 잘 가져온다는 것을 보여줬다("MoleculeNet benchmark
is limited", "only 5 million molecules"). 즉 잘 되는 것은 **근거 제시**이지
**범주 예측**이 아니다. 제품 문구가 "어떤 지적을 받을지"라고 말하는 부분은
데이터가 뒷받침하지 않는다.

**가장 큰 한계 — 정답지가 같은 분류기에서 나온다.** aspect 라벨은 키워드 분류기가
붙인 것이고 그 분류기는 **65.8%를 'other'로 버린다**(§4.3). 라벨이 잡음이면 어떤
랭킹도 이길 수 없고, 위 숫자들은 "파이프라인이 나쁘다"가 아니라 "라벨이 약하다"를
재고 있는 것일 수 있다. **분류기 개선이 다른 모든 개선의 선행 조건**이라는 뜻이다.

그 외: 표본 100~200편·시드 2개, aspect 9개라는 거친 입도, 텍스트 수준 적합성은
측정하지 않음.


## 25. §20을 실제로 돌렸다 — 실행 결과와 커버리지 (2026-08-02)

§20은 구현까지만이고 한 번도 실행하지 않은 상태였다. 이 절은 전 단계를 처음으로
끝까지 돌린 기록이다. 설계가 맞은 곳, 틀린 곳, 그리고 문서가 유도한 사고를 남긴다.

### 25.1 시작하자마자 죽었다 — `init_db.sql`은 마이그레이션이 아니다

`s2_enricher._apply_paper`의 `UPDATE papers SET ... citation_count = %s` 가
`UndefinedColumn`으로 즉시 실패했다. `init_db.sql`에는 `citation_count INT`가 있는데
**운영 DB의 `papers`에는 그 컬럼이 없었다.**

원인은 §20.5에 적어둔 그 문장이다. 코퍼스 테이블은 alembic이 아니라 `init_db.sql`이
만드는데, 이 파일은 **컨테이너 최초 기동 때 한 번만** 실행된다. 즉 파일에 컬럼을
추가해도 이미 살아 있는 DB는 영원히 따라오지 않는다. 볼륨을 지우고 다시 만들지
않는 한 드리프트가 고정된다.

alembic `0008_papers_citation_count`로 복구했다. 두 갈래 모두에서 안전하도록
`ADD COLUMN IF NOT EXISTS`(새 컨테이너는 `init_db.sql`이 이미 만들어 둠) +
`to_regclass` 가드(코퍼스 없이 백엔드만 띄운 DB)를 걸었다.

**교훈: 코퍼스 스키마를 바꿀 때 `init_db.sql`만 고치면 안 된다.** 신규 DB용과
기존 DB용은 서로 다른 경로이고 둘 다 필요하다.

### 25.2 실행 결과

| 단계 | 결과 | 소요 |
|---|---|---|
| arXiv 하베스트 | cs 853,420 + stat 118,986 = **972,406건** (62.4MB gz) | 2h 53m |
| arXiv 매칭 | 제목 일치 25,871 → **확정 25,384** (short_title 307, 동점 포기 487) | 11초 |
| S2 by-arxiv | 대상 25,384 → **매칭 25,330 (99.8%)**, 저자 51,113 | 4분 |
| S2 by-venue | 대상 18,185 → **매칭 4,908**, arxiv_id 642 추가, 저자 6,797 | 12분 |
| 재투고 재계산 | 744 → **747건** (`arxiv_id` 387 / `title_exact` 178 / `fuzzy` 182) | 40초 |

하베스트는 503 재시도가 **0건**이었고, S2는 429가 20건 났지만 전부 백오프로 흡수돼
실패 0으로 끝났다. by-venue의 연속 실패 중단(`MAX_CONSECUTIVE_FAILURES`)은 발동하지
않았다. 최종 상태:

```
papers 43,515편
  arxiv_id        26,026  (59.8%)
  s2_paper_id     30,238  (69.5%)
  citation_count  30,238  (69.5%)
  final_venue     25,426  (58.4%)
authors  s2_author_id  57,910 / 84,270  (68.7%)
citations  0행  (--citations 미실행)
```

매칭 정확도는 상위 인용 논문으로 확인했다 — ViT(2010.11929) 67,188회,
RoBERTa(1907.11692) 30,712회, InstructGPT 22,976회, LoRA 21,508회가 각각 올바른
arXiv id에 붙었다.

### 25.3 커버리지가 당락으로 갈린다 — 그리고 그게 핵심이다

| | 논문 | s2_paper_id | 비율 |
|---|---|---|---|
| 채택 | 22,732 | 22,296 | **98.1%** |
| 탈락·철회·미상 | 20,783 | 7,942 | **38.2%** |

§20.4가 예측한 "S2에는 채택 논문만 학회 venue로 색인되므로 reject/withdrawn은 거의
못 잡는다"가 그대로 확인됐다. by-venue가 채운 4,908편은 사실상 전부 채택 논문이고,
그래서 채택 커버리지만 76.7% → 98.1%로 뛰었다. **탈락 논문은 by-venue로 거의
움직이지 않는다.**

바꿔 말하면 **탈락 논문 7,942편의 S2 데이터는 전량 arXiv 경로로 들어왔다.**
하베스트 3시간의 값어치가 여기 있다 — 이 서비스가 보여주려는 것이 "리젝된 논문이
어떤 지적을 받았나"인데, 그 구간은 arXiv id 없이는 S2에 닿을 방법이 없다.
RoBERTa가 ICLR 2020 **reject**이면서 인용 30,712회로 잡히는 것이 그 사례다.

38.2%가 남은 이유는 재실행으로 해결되지 않는다 — 애초에 arXiv에 없거나(비공개
투고), 제목이 25자 미만이거나, 동명 논문이라 안전하게 포기한 것들이다.

### 25.4 재투고 링크 — 예측이 빗나간 곳

실행 전 예상은 "**신규 링크 0건**, 기존 `title_exact`가 1.00으로 승격될 뿐"이었다.
근거는 `arxiv_matcher`가 `submission_linker`와 **같은 `normalize_title` 완전 일치**를
쓰므로, arXiv id를 공유하는 쌍은 이미 제목으로 잡힌 쌍과 같은 집합이라는 것이었다.
실제로 `arxiv_matcher`만 놓고 보면 이 예상이 맞았다(378쌍 전부 기존 링크).

빗나간 경로는 **by-venue**다. S2가 `externalIds.ArXiv`로 arxiv_id를 642개 역으로
채우는데, S2는 자체 매칭을 쓰므로 **제목이 바뀐 재투고도 같은 arXiv id에 도달한다.**
그 결과 제목 완전 일치 제약을 우회한 8쌍이 잡혔다:

```
Energy-based View of Retrosynthesis            ICLR 2021
  → Towards understanding retrosynthesis by energy…   NeurIPS 2021
Formalizing Generalization and Robustness…     ICLR 2021
  → Formalizing Generalization and Adversarial R…     NeurIPS 2021
Provably Robust Detection of OOD…              ICLR 2022
  → Provably Adversarially Robust Detection of O…     NeurIPS 2022
```

규모는 작다(순증 3건, fuzzy에서 승격 3건). 다만 **제목을 갈아엎은 재투고는 다른
방법으로 잡히지 않는다** — 제목 유사도(trgm 0.7)도 저자 Jaccard도 통과하지 못하는
쌍이다. 재투고 추적을 더 넓히려면 이 경로를 키우는 것이 유일한 방향이다.

### 25.5 남은 것

- **`citations` 0행 유지.** `--citations`를 의도적으로 건너뛰었다. 요청 수가 가장
  많은 단계(`REF_CHUNK=100`)인데, `citations` 테이블을 읽는 코드가 저장소에 아직
  하나도 없다. 소비처가 생길 때 돌리면 된다.
- **`citation_count` / `final_venue`도 아직 아무도 읽지 않는다.** 채워만 뒀고 검색
  랭킹·리포트 어디에도 반영돼 있지 않다.
- **코퍼스 중복 행 228쌍.** 같은 arxiv_id가 **같은 venue+year**에 두 번 들어가 있다
  (예: "Diffusion Models Beat GANs on Image Synthesis" NeurIPS 2021 ×2). 재투고가
  아니라 중복 적재이고, `submission_linker`는 `_different_submission`으로 걸러내므로
  링크에는 영향이 없다. 다만 **검색 결과에 같은 논문이 두 번 뜰 수 있다.**


## 26. 2단계 재정렬로 바꿨다 — 그리고 첫 실호출이 말한 것 (2026-08-06)

§24의 결론("잘 되는 것은 범주 예측이 아니라 근거 제시")을 제품 구조에 반영했다.
유사 논문 선정을 **검색 1단계 → 검색 + LLM 판정 2단계**로 나누고, 통계 레이어를
걷어냈다. 계획과 결정 과정은 docs/추천_파이프라인_재설계.md.

### 26.1 왜 임베딩만으로는 못 고르는가 — §20의 재확인

§20이 "논문별 유사도 점수를 만들 수 없다"고 한 근거(상위 20편 코사인 폭 0.013)는
곧 **"검색이 상위권 안에서 순위를 매기지 못한다"** 는 뜻이기도 하다. 그러면 누가
매길 수 있는가? 본문·실험·참고문헌을 읽을 수 있는 쪽이다.

가중합 재정렬(최신성 0.45)이 그 위에서 순위를 얼마나 흔드는지 재봤다. 2024년 이후
논문 40편을 쿼리로, 순수 RRF 순서 대비 가중합 순서를 비교했다:

```
계층        중앙값  p90 |  N=30   N=50   N=100
rank 1          5  144 |   85%    85%     90%
rank 1-5        8  123 |   70%    76%     88%
```

**그런데 코사인은 거의 안 떨어진다.** 같은 표본에서 top-30 평균이 0.9409 → 0.9370
(-0.0039), 최저는 -0.0008, 대신 평균 1.2년 최신이다. -0.004는 §20이 "순위를 정당화할
수 없다"고 판정한 폭보다도 작다.

**두 사실은 모순이 아니라 같은 말이다** — 순위는 크게 바뀌는데 우리가 가진 척도로는
그 차이를 볼 수 없다. 즉 최신성 가중치는 유사도를 유의미하게 희생하지 않으며
("어느 정도 유사하면 최신 논문의 리뷰가 더 가치 있다"는 요구는 거의 공짜로 달성된다),
동시에 **그 구간을 판정할 다른 눈이 필요하다**는 뜻이다.

### 26.2 구조

```
PDF → 검색(후보 50편, 리뷰 보유 43,034편만) → Sonnet 5 판정(최대 5편) → 리뷰 조회 → 종합
```

- PDF를 **텍스트로 추출하지 않고 document 블록으로** 넘긴다. 실측 결과 페이지당
  텍스트의 2.03배(26p=77,168토큰)지만, 표·수식·2단 조판과 참고문헌 목록이 깨지는
  문제를 통째로 우회한다.
- PDF를 프롬프트 **프리픽스 맨 앞**에 고정하고 `cache_control`을 건다. 종합 호출에서
  78K가 캐시 읽기로 넘어가며 그 부분이 $0.195 → $0.016이 됐다(실측).
- 후보에 없는 `paper_id`는 버린다. §23.5의 인용 검증과 같은 규율이다 — 검증이 없으면
  지어낸 논문의 리뷰를 조회하고, 조회가 비면 사용자에게는 그냥 빈 카드로 보인다.
- 검색 신뢰도가 weak이면 **LLM을 부르지 않는다.** 도메인 밖 PDF에도 모델은 후보 중
  '가장 비슷한 5편'을 성실히 골라낸다.

### 26.3 첫 실호출 — 재정렬이 실제로 순위를 뒤집었다 ★

LoRA 논문(arXiv 2106.09685) 26페이지 + 후보 50편, Sonnet 5, 2회:

```
1회차: 검색 15, 42,  4, 34, 38위
2회차: 검색 15, 42, 47,  4,  7위
```

- 검색 상위 10위에서 고른 것은 5편 중 1~2편뿐이다.
- 두 회차 모두 **1순위로 검색 15위를 올렸다.** 그 논문은 입력과 *동일한* 논문의
  ICLR 2022 투고본이다 — 최신성 가중치가 2022년이라는 이유로 15위로 내린 것을
  모델이 되돌렸다. **코사인은 "동일한 논문"과 "매우 비슷한 논문"도 구분하지 못한다**는
  §26.1의 직접적인 증거다.
- 무관한 후보(단백질 구조 논문 10편)를 주자 **0편을 골랐다**(출력 9토큰). "적게
  골라라"가 프롬프트상의 말이 아니라 실제 동작이다.

⚠️ **표본 1편·2회다.** 자기 자신이 코퍼스에 있는 특수 상황이기도 하다. 다만 선택이
42·47위에서 나온 것은 **후보 50편이 모자랄 수 있다**는 뜻이고, 이것이 현재 가장 큰
열린 위험이다.

### 26.4 무엇을 잃었나

통계 레이어(§18의 lift·Fisher 당락 대조, §19의 rating 맥락, venue 경향)를 제거했다.
5편 위에서는 lift도 Fisher도 통계적으로 무의미하기 때문이다. 집계 코드
(`graph/clustering.py`)와 배치(`scripts/build_*.py`), `aspect_base_rates`·`venue_stats`
테이블은 남겨 뒀다 — 되살릴 때의 출발점이고, `eval_retrieval.py`가 여전히 쓴다.

**그리고 평가를 잃었다.** §24의 held-out 평가는 aspect 예측을 재는데 그 과제가 목표에서
빠졌다. "두 논문이 정말 비슷한가"에는 사람 라벨 없이 쓸 수 있는 정답지가 없다 — §24가
가능했던 것은 aspect 라벨이 이미 붙어 있었기 때문이다. 대신 후보 50편과 선정 결과를
`similar_paper_matches`에 전부 남긴다. 실사용 분석이 그대로 평가 데이터가 되는 구조이고,
현실적으로 확보 가능한 유일한 대규모 신호다.


### 26.5 코퍼스 중복 — 지우지 않고 검색에서 접었다 (2026-08-06)

§25.5의 "중복 행 정리"를 처리하려고 들여다봤다가 **정리하면 안 되는 것**임을 알았다.

**실측** (`lower(btrim(title))`, venue, year 기준):

```
중복 그룹 302개, 관련 논문 604편, 최대 중복 수 2
그중 301개가 NeurIPS 2021 하나에 몰려 있다 (그 venue 2,768편의 21.7%)
```

쌍의 성격:

- `openreview_id`도 `forum_id`도 **다르다** — 별개의 OpenReview 노트다.
- `decision`은 **301쌍 전부 일치**한다 (accept-poster 268, spotlight 25, oral 5, reject 3).
- **리뷰가 하나도 겹치지 않는다** — 301쌍 전부에서 공유하는 review `openreview_id`가
  0건이고, 쌍당 리뷰 합계가 평균 7.8건이다.
- 어느 쪽이 정본인지 판별되지 않는다. 리뷰 수는 낮은 id가 적은 쌍 66 / 많은 쌍 65 /
  같은 쌍 170으로 대칭이고, 평균 평점도 5.99 vs 6.07로 차이가 없다.

**따라서 한 행을 지우면 실제 리뷰 약 1,170건이 사라진다.** 그것도 어느 쪽을 지울지
근거 없이. 이건 중복 제거가 아니라 데이터 손실이다.

**대신 검색에서만 접는다** (`hybrid_search._pick_without_duplicates`). 제품에서 실제로
아픈 지점은 좁다 — 후보 50편에 같은 논문이 두 번 들어가면 자리가 낭비되고, LLM이 둘 다
고르면 사용자에게 같은 논문이 두 번 뜬다. **`paper_id`가 다르므로 선정 단계의 중복
제거로는 걸리지 않는다.**

실측으로 확인한 문제와 수정 효과 (같은 방식으로 뽑은 표본 8쌍):

```
수정 전: 후보 50편에 쌍이 통째로 들어온 경우 6/8
수정 후: 0/8 (전부 한 편만), 후보 수는 50 유지
```

남길 쪽은 **리뷰가 많은 행**으로 한다(표본 5건 전부 의도대로 동작). 리뷰가 겹치지
않으므로 어느 쪽을 고르냐가 곧 사용자가 볼 리뷰 수다. venue를 키에 넣어 ICLR 2024 →
NeurIPS 2024 재투고는 접지 않는다 — 같은 제목이지만 다른 심사이고, `submission_links`가
추적하는 바로 그 관계다.

⚠️ **근본 원인은 아직 모른다.** 왜 NeurIPS 2021만, 왜 21.7%인지 확인하려면 OpenReview
API로 두 forum이 실제로 존재하는지 봐야 한다. 저장을 건드리지 않았으므로 나중에
밝혀지면 그때 판단하면 된다.
