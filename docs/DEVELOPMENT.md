# DEVELOPMENT

## 1. 프로젝트 개요

**AICE**는 ML/AI 논문을 위한 리서치 어시스턴트입니다. 백엔드(FastAPI)와 AI 분석
파이프라인이 **한 저장소, 한 프로세스, 한 DB**로 통합돼 있습니다.

사용자가 자신의 논문을 올리면, 비슷한 기존 논문을 찾아 **"그 논문들이 실제로 어떤 지적을
받았는지"** 를 리뷰 원문과 함께 보여줍니다.

**예측이 아니라 열람입니다.** 유사 논문으로 지적 범주를 *예측*하는 것은 '검색 없음'
베이스라인에 졌지만(설계서 §24), 구체적인 지적 *문장*을 근거로 가져오는 것은 잘 됩니다.
그래서 제품이 하는 말은 "당신은 X를 지적받을 것입니다"가 아니라 **"비슷한 논문들은 이런
지적을 받았습니다"** 입니다. 이 경계는 API 응답 필드부터 화면 문구까지 전부 지켜져야 합니다.

결과가 정답처럼 보이지 않도록, 항상 어떤 유사 논문·리뷰를 근거로 삼았는지와 **그 결과를
믿어도 되는지(신뢰도)** 를 함께 노출하는 것이 설계 원칙입니다.

### 파이프라인 2단계 개편 (2026-08-06 완료)

유사 논문 선정을 검색 1단계에서 **검색 + LLM 판정 2단계**로 바꿨습니다. 설계 근거와
실측 수치는 [추천_파이프라인_재설계.md](추천_파이프라인_재설계.md)에 있습니다.

| 영역 | 무엇이 바뀌었나 |
|---|---|
| 입력 | **PDF 전용.** `POST /api/submissions`(JSON) 삭제, 추출 실패 시 422, 60p 초과 거부 |
| 검색 | 후보 20편 → **50편**, 리뷰 보유 논문(43,034편)만, `CANDIDATE_POOL` 50 → 100 |
| 선정 | **LLM 재정렬 신설** — PDF 원본 + 후보 50편을 Sonnet 5에 넘겨 최대 5편 (`effort=high`) |
| 조회 | 선정 5편의 **리뷰 전문·AC 총평·평점**을 Report에 함께 실음 |
| 분석 | **통계 레이어 제거** — `review_patterns`·`venue_trends`·`rating_context`·`resubmission_flows` |
| 저장 | `submissions.pdf_bytes`(deferred)·`page_count`, `similar_paper_matches`에 선정 결과 (alembic 0011) |
| 비용 | 분석 1회 약 **$0.30** (Sonnet 5 도입가, 26p PDF 기준. 2026-09-01부터 약 1.5배) |

**남은 것**: `eval_retrieval.py`가 재던 aspect 예측이 목표에서 빠졌고 그 자리를 채울
자동 평가가 없습니다. 그리고 실호출에서 LLM이 검색 **47위**를 고른 사례가 나와
후보 50편이 모자랄 가능성이 열려 있습니다 (재설계 문서 §4.4.1, §8).

## 2. 기술 스택

| 영역 | 사용 기술 |
|---|---|
| 웹 프레임워크 | FastAPI 0.115 |
| ASGI 서버 | Uvicorn |
| ORM | SQLAlchemy 2.0 |
| 마이그레이션 | Alembic (서비스 테이블만) |
| DB | PostgreSQL 17 + **pgvector** (psycopg3) |
| 데이터 검증 | Pydantic v2 (+ pydantic-settings, email-validator) |
| 인증 | python-jose(JWT) + passlib[bcrypt] |
| 임베딩 | SPECTER2 (torch + transformers + adapters, CPU) |
| 분석 파이프라인 | LangGraph (고정 DAG), Anthropic Claude (선택) |
| 논문 데이터 수집 | requests 기반 자체 OpenReview 클라이언트 |

Python **3.13+** 기준입니다 (3.14 확인). `psycopg2-binary`와 `openreview-py`는 제거했습니다
(각각 3.13 휠 부재 / 미사용).

## 3. 폴더 구조

```
AICE/
├── app/                      # 백엔드 (FastAPI)
│   ├── main.py                 # 앱 진입점 (미들웨어/에러 핸들러/라우터 등록, 워밍업)
│   ├── database.py             # DB 엔진/세션 + psycopg3 방언 변환
│   ├── core/
│   │   ├── config.py            # 백엔드 전용 설정 (공유 값은 paper_assistant/config.py)
│   │   ├── security.py          # 비밀번호 해시 + JWT 생성/검증
│   │   ├── deps.py              # get_current_user 등 Depends 함수
│   │   ├── errors.py            # 전역 예외 핸들러 (응답 포맷 통일)
│   │   ├── mail.py               # 비밀번호 재설정 메일 발송
│   │   ├── legal.py              # 약관·개인정보처리방침 원문 로드(app/legal/*.md)
│   │   ├── middleware.py         # 요청 단위 미들웨어
│   │   └── google_oauth.py       # 구글 id_token 검증
│   ├── models/                 # SQLAlchemy 모델 — 서비스 테이블만
│   │   ├── user.py               # users
│   │   ├── submission.py         # submissions
│   │   ├── analysis.py           # review_predictions, similar_paper_matches
│   │   ├── onboarding.py         # onboarding_profiles
│   │   └── share.py              # submission_shares
│   ├── routers/
│   │   ├── auth.py               # 회원가입/로그인/구글 로그인/refresh/logout/비밀번호 재설정
│   │   ├── user.py               # 내 정보 조회/수정/탈퇴, 온보딩 조회/수정, 약관 재동의
│   │   ├── submissions.py        # 내 논문 업로드(PDF)·조회·삭제 + 분석 시작/조회 + 공유 (핵심)
│   │   ├── corpus.py             # 코퍼스 논문 목록/상세/리뷰/수정 이력 (AI 파트 위임)
│   │   ├── onboarding.py         # 회원가입 전 익명 온보딩 답변 저장
│   │   ├── legal.py              # 약관·개인정보처리방침 원문 조회 (인증 불필요)
│   │   └── shared.py             # 공유 토큰으로 비로그인 공개 조회
│   ├── schemas/                # Pydantic 요청/응답 스키마
│   │   ├── common.py             # ApiResponse[T]
│   │   ├── auth.py / submission.py / analysis.py
│   │   ├── corpus.py             # AI 파트 스키마 재수출 (중복 정의 금지)
│   │   ├── share.py              # 공유 링크 발급/공개 응답
│   │   └── legal.py              # 약관 문서 응답
│   └── services/               # 도메인 규칙 — 라우터에는 HTTP 관심사만 남긴다
│       ├── submissions.py        # 업로드 검증(용량·페이지·길이)·추출·저장, 소유권
│       ├── analysis.py         # ★ 백엔드와 AI 파트가 만나는 유일한 지점
│       └── shares.py           # 공유 토큰 발급/폐기/조회
├── paper_assistant/          # AI 파트 (공개 API 10개)
│   ├── config.py               # ★ 공유 환경설정의 단일 소스
│   ├── schemas.py              # Report 등 통합 계약 스키마
│   ├── llm.py                  # Claude 래퍼 — graph/·query/·pdf/가 함께 쓴다
│   ├── query/                  # 조회 전용 (detail, revisions, journey/timeline/narrative/story)
│   ├── graph/                  # LangGraph 고정 DAG (input→retrieval→llm_rerank→review_fetch→synthesis)
│   ├── retrieval/              # 하이브리드 검색 (벡터 + 전문검색 RRF)
│   ├── embedding/              # SPECTER2
│   ├── ingest/                 # OpenReview 수집 + 정규화 + arXiv/S2 보강
│   │   └── _http.py              # 세 API 클라이언트가 공유하는 재시도 뼈대
│   ├── pdf/                    # PDF에서 제목/초록 추출
│   └── db/                     # 커넥션 풀 + 적재 + 코퍼스 통계 캐시(stats.py)
├── scripts/                  # 코퍼스 스키마(init_db.sql) + 운영 배치 + cleanup_stale_onboarding.py
├── tests/
│   ├── app/                    # 백엔드 (인증·구글 로그인·소유권·분석 상태 전이)
│   ├── paper_assistant/        # AI 파트
│   └── meta/                   # 설정·경계 드리프트 (소스만 읽으므로 DB 불필요)
├── docs/                     # 설계서·팀 공유 문서·개발 문서
├── alembic/versions/         # 0001 초기 테이블 … 0018 공유 링크 (아래 §4 참고)
├── docker-compose.yml        # pgvector Postgres (포트 5433)
├── pyproject.toml            # pytest 설정 (pythonpath)
├── requirements.txt          # 런타임 의존성
└── requirements-dev.txt      # + pytest, httpx
```

## 4. 데이터 모델

DB는 하나지만 **소유자가 둘로 나뉩니다.** 이 경계를 넘지 않는 것이 중요합니다.

```
[ 서비스 테이블 — alembic이 관리 ]        [ 논문 코퍼스 — scripts/init_db.sql이 관리 ]

users ──< submissions                     papers ──< reviews ──< review_points
  │           │    │                        │  └──< paper_authors >── authors
  │           │    └──< submission_shares   │  └──< submission_links (재투고 흐름)
  │           └──< review_predictions       venue_stats, aspect_base_rates, ingest_status
  │                     │
  │                     └──< similar_paper_matches ┄┄(paper_id, FK 없음)┄┄> papers
  └──< onboarding_profiles (1:1, user_id nullable — 회원가입 전엔 주인 없음)

[ 코퍼스 캐시 — AI 파트가 조건부 생성(papers 테이블 있을 때만) ]
paper_stories ┄┄(paper_id, FK 없음)┄┄> papers        # /story 결과 캐시
paper_body_diffs ┄┄(paper_id, FK 없음)┄┄> papers     # /revisions/body-diff 결과 캐시
```

### 서비스 테이블
- **users**: 회원. 이메일/비밀번호 또는 구글(`google_sub`) 인증. `openreview_id`는
  가입 경로와 무관하게 필수이며 **unique**입니다(서비스가 OpenReview 코퍼스 기반이라
  신원 값으로 사용 — 두 계정이 같은 OpenReview 계정을 자처할 수 없게 잠갔습니다,
  `alembic/versions/0006_unique_openreview_id.py`). `password_hash`는 구글 전용
  계정이 있어 nullable입니다 (`alembic/versions/0004_add_google_and_openreview_id.py`).
  `token_version`은 refresh_token 폐기용 버전 카운터로, 로그아웃 시 증가시켜 그
  이전에 발급된 refresh_token을 전부 무효화합니다
  (`alembic/versions/0003_add_user_token_version.py`). `terms_agreed_at`·
  `terms_version`·`privacy_version`은 약관 재동의(`POST /api/user/me/consent`)
  추적용입니다 (`alembic/versions/0015_terms_consent.py`).
- **submissions**: 사용자가 올린 내 논문. **PDF 원본을 `pdf_bytes`에 저장합니다** —
  분석이 BackgroundTasks라 응답 이후에 도는데, 2단계 LLM이 본문·참고문헌을 봐야 하기
  때문입니다. ⚠️ `deferred=True`로 매핑돼 있습니다(최대 20MB 블롭이라, 아니면 목록
  조회가 행마다 이걸 끌어옵니다). 임베딩은 저장하지 않고 분석할 때마다 계산합니다.
- **review_predictions**: 분석 1회분. 백그라운드 작업의 상태(`pending/running/done/failed`)이자
  결과 저장소로, 분석 결과 전체가 `report` JSONB에 들어갑니다. `progress` JSONB
  컬럼(`alembic/versions/0014_analysis_progress.py`)에 폴링용 진행 단계가 담깁니다.
- **similar_paper_matches**: 검색 후보 **50편 전부**와 그중 LLM이 고른 것
  (`rank`=검색 순위, `selected`, `llm_rank`=화면 순서, `selection_reason`).
  후보까지 남기는 이유는 **"검색이 뽑은 것"과 "LLM이 고른 것"의 관계가 이 파이프라인의
  품질 지표**이기 때문입니다 — 유사도에는 사람 라벨 없는 정답지가 없어서, 실사용에서
  모이는 이 신호가 현실적으로 유일한 대규모 평가 데이터입니다.
- **onboarding_profiles**: 회원가입 전 익명 상태에서 저장하는 온보딩 답변. `user_id`는
  처음엔 null이고, 회원가입 요청(`SignupRequest.onboarding_id`)이 이 id를 실어 보내면
  그때 연결됩니다 — 세션 쿠키 없이 스테이트리스 구조를 유지하기 위한 설계입니다
  (`alembic/versions/0005_add_onboarding_profiles.py`). 회원가입 없이 이탈한
  미연결 행은 `scripts/cleanup_stale_onboarding.py`로 주기적으로 정리하세요
  (기본 30일 지난 행 삭제, `--dry-run`으로 미리 확인 가능). 로그인 후에는
  `PATCH /api/user/me/onboarding`(마이페이지)으로도 upsert됩니다(아래 §7).
  `venue`는 원래 문자열 하나였다가 다중 선택 리스트(JSONB)로 바뀌었고, `similarity_focus`·
  `recency_bias`가 분석 파이프라인에 실제로 쓰이도록 추가됐습니다(`alembic 0016`).
  쓰이지 않던 `purposes`·`result_order`·`stage`는 `alembic 0017`에서 제거했습니다.
- **submission_shares**: 로그인 없이 열람 가능한 공개 공유 링크의 토큰
  (`token`은 `secrets.token_urlsafe`, `revoked_at`으로 폐기 여부 관리,
  `alembic/versions/0018_submission_shares.py`). `GET /api/shared/{token}`이 이
  테이블로 소유자 정보 없이 `title`·`abstract`·`field`·`report`만 돌려줍니다.
- **paper_stories / paper_body_diffs**: `/story`와 `/revisions/body-diff` 결과 캐시.
  alembic이 관리하지만(`0009_paper_stories_cache.py`, `0013_paper_body_diffs_cache.py`)
  `papers` 테이블(코퍼스)이 있을 때만 조건부로 만들어집니다 — 서비스 DB와 코퍼스 DB가
  분리된 환경(코퍼스 없이 서버만 띄운 경우)에서도 `alembic upgrade head`가 깨지지
  않게 하기 위해서입니다.

### 논문 코퍼스 (AI 파트 소유, 43,515편)
- **papers / reviews / review_points**: ICLR 2020–2025 + NeurIPS 2021–2024에서 수집한
  논문·리뷰·개별 지적 항목 (리뷰 168,217건, 지적항목 119만 건).
- **venue_stats / aspect_base_rates**: 학회별 점수 기준선과 코퍼스 전체 지적 비율.
  ⚠️ **분석 경로는 더 이상 읽지 않습니다**(통계 레이어 제거). `venue_stats`는 `/story`가,
  `aspect_base_rates`는 `scripts/eval_retrieval.py`가 쓰므로 남겨 뒀습니다.
- **submission_links**: 같은 논문의 재투고 추적 (ICLR reject → NeurIPS accept). 747건.
- **arXiv/S2 보강 필드** (`papers.arxiv_id`·`s2_paper_id`·`citation_count`):
  2026-08-02에 채웠습니다(설계서 §25). **코퍼스 전체를 덮지 않습니다** —
  `s2_paper_id` 기준 채택 논문 98.1% / 탈락 논문 38.2%이고, 전체로는 69.5%입니다.
  `citation_count`는 `citation_percentile`(같은 연도 내 백분위, alembic 0010)로
  환산되어 검색 랭킹에 실제로 반영됩니다 — 결측이면 중립값 0.5로 대체합니다.
  아무도 읽지 않던 `final_venue`·`authors.s2_author_id`·`citations`는 alembic
  0012에서 제거했습니다(아래 표).

코퍼스 테이블에는 SQLAlchemy 모델이 없습니다. `alembic/env.py`의 `CORPUS_TABLES`가
autogenerate 대상에서도 제외하므로, 백엔드가 마이그레이션을 만들어도 코퍼스를 건드리지
않습니다. 코퍼스 조회가 필요하면 `paper_assistant.get_paper_detail()`을 쓰세요.

### 통합하면서 없앤 테이블/컬럼 (전부 의도된 변경)
| 대상 | 이유 |
|---|---|
| `papers`, `reviews` (백엔드판) | 코퍼스 쪽이 정본. UUID/TEXT 스키마로는 43,515편과 벡터 검색을 담을 수 없음 |
| `revisions` | 저장할 수가 없음 — papers는 openreview_id로 upsert라 최신 버전만 남음. 대신 `GET /api/papers/{id}/revisions`가 OpenReview를 실시간 조회 |
| `similar_paper_matches.similarity_score` | 논문별 유사도 점수는 만들 수 없음 (아래 6번) → `rank` + `match_type`으로 대체 |
| `submissions.embedding` | 저장하려면 vector(768)이어야 하는데, 재사용 이득보다 스키마 결합이 큼 |

### 스키마 전수 조사로 걷어낸 것 (alembic 0012, 2026-08-07)
판정 기준은 **SELECT 하는 코드가 저장소에 있는가** 하나였습니다. 읽는 쪽이 없으면
채우는 비용만 나가고, 다음 사람이 "이미 채워진 줄 알고" 읽으려 듭니다.

| 대상 | 이유 |
|---|---|
| `submissions.content` | 텍스트 붙여넣기 경로가 사라진 뒤 항상 NULL. ⚠️ **`SubmissionResponse`에서 필드가 없어졌습니다** — 프론트가 읽고 있었다면 지우세요 |
| `reviews.raw_content` | 선언만 있고 INSERT된 적이 없음 |
| `reviews.points_extracted` + `reviews_pending` 인덱스 | true로 세팅만 하고 WHERE에 쓰는 곳이 없음. 수집 재개는 `ingest_status`가 담당 |
| `review_points.embedding` | 지적항목은 쿼리 시점에 임베딩하므로(§13) 늘 NULL |
| `citations` 테이블 | 적재 코드만 있고 조회가 0건. 인용 그래프를 쓸 계획 없음 (실측 0행) |
| `papers.final_venue` | 같은 개념을 `query/journey.py`가 `submission_links`로 계산함. 컬럼 쪽은 소비자 없음 (25,426행 폐기) |
| `authors.s2_author_id` + `authors_s2` 인덱스 | 성(姓) 매칭으로 채웠지만 읽는 곳 없음 (57,910행 폐기) |
| `similar_paper_matches_selected` 인덱스 | 0011이 "선정 5편만 꺼내는 조회"용으로 만들었지만, 유일한 조회 `matches_for()`에 `WHERE selected`가 없어 쓰일 수 없었음 |
| `similar_paper_matches_paper` 인덱스 | `paper_id`로 거르는 쿼리가 없음 |
| `papers_decision` 인덱스 | `WHERE decision` 술어가 없고 값이 9종뿐이라 플래너가 고르지 않음 |

`final_venue`·`s2_author_id`는 S2 호출로 채운 실데이터를 버립니다. 되살리려면
`s2_enricher`를 다시 돌려야 하고 API 호출이 다시 듭니다(그래서 downgrade는 컬럼만
되돌리고 값은 NULL입니다).

## 5. 로컬 실행 방법

[README.md](../README.md)의 "로컬 실행"을 따르세요. 기존과 달라진 점만 요약하면:

- DB는 로컬 PostgreSQL이 아니라 `docker compose up -d`로 띄우는 **pgvector 인스턴스(5433)** 입니다.
- 논문 코퍼스는 git에 없습니다. `scripts/restore_db.sh`로 DB 덤프를 복원해야 합니다.
- `alembic upgrade head`는 서비스 테이블 6개(`users`, `submissions`,
  `review_predictions`, `similar_paper_matches`, `onboarding_profiles`,
  `submission_shares`)를 만듭니다. `paper_stories`·`paper_body_diffs`는 코퍼스가
  이미 복원돼 있을 때만 조건부로 추가됩니다(위 §4).

## 6. 프론트/백엔드가 반드시 지켜야 할 것

AI 파트가 실측으로 확인한 함정입니다. 전부 실제로 부딪혀서 고친 것들이라, `Report`를
화면에 옮길 때 같은 실수를 반복하지 않으려면 이 절만은 읽어야 합니다.

### 먼저 — 이 서비스가 할 수 있는 말

**예측형 문구를 쓰면 안 됩니다.** "이런 지적을 받을 것입니다", "예상 리뷰", "당신의
논문은 novelty가 약하다고 평가될 것입니다" 같은 표현은 데이터가 뒷받침하지 않습니다(§24).
**주어는 항상 유사 논문입니다** — "비슷한 논문 N편은 이런 지적을 받았습니다".

이건 표현의 문제가 아니라 무엇이 검증됐는지의 문제입니다. 지적 *범주* 예측은 '검색 없음'
베이스라인에 0.66~0.78배로 졌고, 잘 되는 것은 구체적인 지적 *문장*을 근거로 가져오는
쪽입니다. 화면이 예측을 약속하면 검증되지 않은 것을 파는 것이 됩니다.

### 그다음 네 가지 — 숫자를 화면에 옮길 때

**① 유사도 점수는 만들 수 없습니다.**
SPECTER2 코사인 유사도는 상위 20편 안에서 폭이 **0.013**밖에 안 됩니다 — 1위든 20위든
사실상 같은 값이라, 어떤 변환을 해도 순위를 정당화할 점수가 나오지 않습니다.
→ `similar_papers[]`에는 점수 대신 `rank`와 `match_type`(`semantic`/`lexical`/`both` —
왜 걸렸는지)이 들어갑니다. **"유사도 92%" 같은 UI는 만들면 안 됩니다.**

**② 대신 "이 검색 결과를 믿어도 되는지"는 잘 갈립니다.**
논문 개별 점수는 못 갈라도, 쿼리가 우리 도메인(ML/AI 논문) 안에 있는지는 뚜렷합니다 —
도메인 안은 top-5 평균 코사인 **0.946~0.966**, 밖은 **0.852~0.867**로 겹치지 않습니다.
→ `confidence.level`(strong/moderate/weak)과 `is_reliable`을 확인해 **`weak`이면 경고
배너가 필수**입니다. 없으면 요리 레시피를 넣어도 ML 논문을 자신 있게 내놓습니다.
`weak`이면 LLM 재정렬 자체를 건너뛰므로 `selected_papers`도 비어 옵니다.

**③ `selected_papers`가 결과이고 `similar_papers`는 후보 풀입니다.**
후보 50편은 근거 추적용 기록이지 "유사 논문 목록"이 아닙니다. 특히 `selected_papers`가
**비어 있을 때 후보로 채우면 안 됩니다** — 본문까지 대조해 비슷하지 않다고 판정한
결과이고, 채우는 순간 "비슷한 논문이 받은 리뷰"라는 약속이 거짓이 됩니다.
→ 화면은 `selected_papers`만 그리고, 후보는 접어서 "여기 있다고 비슷한 논문은 아니다"를
명시하세요.

**④ 리뷰 점수는 논문끼리 비교하면 안 되고, 미분리 리뷰는 '지적'이 아닙니다.**
- 척도가 학회마다 다릅니다 (ICLR 2020만 1~8점, 나머지는 1~10점). `avg_rating`을
  논문 간에 비교하지 마세요. 쓸 수 있는 것은 `rating_spread`(크면 리뷰어 의견이 갈림)입니다.
- **`reviews[].is_unsplit`이 참이면 `weaknesses`에 리뷰 본문 전체가 들어 있습니다.**
  2023년 이전 학회가 전부 여기 해당하고, '지적받은 점'이라 라벨을 붙이면 리뷰 전체가
  지적으로 둔갑합니다 — '리뷰 본문'으로 한 덩어리 표시하세요.
- 5편으로 채택률을 계산하지 마세요. 유사도로 고른 5편은 어떤 것의 표본도 아닙니다.

## 7. 구현 완료 API 목록

⚠️ **요청/응답 필드의 정확한 타입·필수 여부는 여기가 아니라 Swagger가 정본입니다**
(`/docs`, `/openapi.json` — FastAPI가 라우터에서 자동 생성하므로 코드와 어긋날 수
없습니다). 아래 표는 필드 나열이 아니라 **Swagger에 안 나오는 것**(멱등성·rate
limit·왜 이런 상태 코드를 쓰는지 같은 동작 근거)을 남기는 용도입니다. 새 엔드포인트를
추가했다면 이 표에도 한 줄 추가하고 [CHANGELOG.md](../CHANGELOG.md)에도 적으세요 —
둘 다 손으로 관리되므로 자동으로 따라오지 않습니다.

모든 응답은 `{ "success": bool, "data": ..., "error": { "code", "message" } | null }` 형태로
통일돼 있습니다 (`app/schemas/common.py`의 `ApiResponse[T]`).

| 도메인 | 메서드/경로 | 설명 | 인증 |
|---|---|---|---|
| Auth | `POST /api/auth/signup` | 회원가입 (openreview_id 필수, onboarding_id로 온보딩 답변 연결 선택) | - |
| Auth | `POST /api/auth/login` | 로그인, access_token + refresh_token 발급 | - |
| Auth | `POST /api/auth/google` | 구글 로그인/연동 (id_token 검증, 신규 가입 시 openreview_id 필수) | - |
| Auth | `POST /api/auth/refresh` | refresh_token으로 access_token 재발급 (refresh_token도 회전) | - |
| Auth | `POST /api/auth/logout` | 로그아웃 (User.token_version 증가 → 이전 refresh_token 전부 무효화) | 필요 |
| Auth | `POST /api/auth/password/forgot` | 비밀번호 재설정 메일 발송 (계정 존재 여부는 알리지 않음) | - |
| Auth | `POST /api/auth/password/reset` | 재설정 토큰으로 비밀번호 변경 | - |
| User | `GET /api/user/me` | 내 정보 조회 | 필요 |
| User | `PATCH /api/user/me` | 내 정보 수정 (nickname, openreview_id, 비밀번호) | 필요 |
| User | `DELETE /api/user/me` | 회원 탈퇴 (submissions 이하 CASCADE 삭제) | 필요 |
| User | `POST /api/user/me/consent` | 개정된 약관·개인정보처리방침에 재동의 (body 없음, 호출 자체가 동의) | 필요 |
| User | `GET /api/user/me/onboarding` | 내 온보딩 답변 조회 (마이페이지) | 필요 |
| User | `PATCH /api/user/me/onboarding` | 내 온보딩 답변 수정 (없으면 upsert로 생성, 보낸 필드만 갱신) | 필요 |
| Legal | `GET /api/legal/{document}` | 약관("terms")·개인정보처리방침("privacy") 원문 조회. IP 기준 **60회/분** | - |
| Submission | `POST /api/submissions/pdf` | 내 논문 업로드 — **유일한 입력 경로**. title/abstract가 비면 PDF에서 추출, 응답에 `page_count`. 추출 실패·60p 초과는 422, **20MB 초과는 413** | 필요 |
| Submission | `GET /api/submissions` | 내 초안 목록 (최신순, 본문 제외) | 필요 |
| Submission | `GET /api/submissions/{id}` | 초안 상세 | 필요 |
| Submission | `DELETE /api/submissions/{id}` | 초안 삭제 (분석 결과도 함께) → **204** | 필요 |
| Submission | `POST /api/submissions/{id}/analysis` | 분석 시작 → **202**, status=pending | 필요 |
| Submission | `GET /api/submissions/{id}/analysis` | 분석 상태/결과 조회 (폴링) | 필요 |
| Share | `POST /api/submissions/{id}/share` | 공개 공유 링크 발급 → `{token, url}`. **이미 있으면 그것을 반환**(멱등, 200). 분석이 `done`이 아니면 **409** | 필요 |
| Share | `DELETE /api/submissions/{id}/share` | 공유 폐기 → **204**. 폐기할 것이 없어도 204 (멱등) | 필요 |
| Share | `GET /api/shared/{token}` | **비로그인 공개 조회.** 없는·폐기된 토큰은 전부 **404**(사유를 구분하지 않음). IP 기준 **300회/시간** | **불필요** |
| Corpus | `GET /api/papers` | 코퍼스 논문 목록 (venue/year/field/q 필터, limit/offset 페이지네이션) | - |
| Corpus | `GET /api/papers/{paper_id}` | 코퍼스 논문 상세 (초록·리뷰 전문·지적 항목) | - |
| Corpus | `GET /api/papers/{paper_id}/reviews` | 그 논문의 리뷰 목록 | - |
| Corpus | `GET /api/papers/{paper_id}/revisions` | 저자 수정 이력 (**외부 API 실시간 조회**). IP 기준 **100회/시간** | - |
| Corpus | `GET /api/papers/{paper_id}/revisions/body-diff` | `/revisions`에 pdf 교체 지점의 본문 전체 단어 단위 diff를 얹은 버전 (`paper_body_diffs`에 캐시, LLM 미사용). IP 기준 **30회/시간**, `refresh=true`는 **로그인 필요** | -<br>(refresh만 필요) |
| Corpus | `GET /api/papers/{paper_id}/story` | 심사 서사 — 재투고 궤적 + 리뷰·응답·수정 타임라인 + 요약 (**외부 API + LLM**, `paper_stories`에 캐시). IP 기준 **100회/시간**, `refresh=true`는 **로그인 필요** | -<br>(refresh만 필요) |
| Onboarding | `POST /api/onboarding` | 회원가입 전 익명 온보딩 답변 저장 | - |

⚠️ `paper_id`는 UUID가 아니라 **BIGINT**입니다 (코퍼스가 BIGSERIAL). 분석 결과의
`similar_papers[].paper_id`를 그대로 넘기면 됩니다.

⚠️ `GET /api/shared/{token}`은 **이 서비스에서 유일하게 인증이 없는 조회 경로**입니다.
응답(`SharedAnalysisResponse`)은 `title`·`abstract`·`field`·`report` 넷뿐이고,
`user_id`·이메일·`pdf_bytes`·`submission_id`는 의도적으로 빠져 있습니다. 필드를
추가하는 것은 곧 인터넷 전체에 공개하는 것이므로, 늘리기 전에
`app/schemas/share.py`의 docstring을 먼저 읽으세요. `url`은 서버가
`FRONTEND_BASE_URL`로 조립하므로 **프론트가 다시 만들지 마세요** — 규칙이 두 곳에
생기면 한쪽만 바뀌었을 때 이미 나간 링크가 깨집니다.

남의 초안에 접근하면 403이 아니라 **404**입니다 — 존재 여부 자체를 알리지 않습니다.

⚠️ 리뷰 목록 경로가 `GET /api/reviews?paper_id=`에서 `GET /api/papers/{paper_id}/reviews`로
바뀌었습니다 (같은 리소스의 하위 경로가 맞고, 예전 경로는 상세 조회를 통째로 돌린 뒤
리뷰만 꺼내 쓰느라 불필요한 쿼리 3개를 더 날렸습니다).

기존 `POST /api/feedback/predictions`(501 반환)는 분석 시작/조회 두 개로 대체돼 삭제됐습니다.

refresh_token은 JWT라 상태가 없어 개별 폐기가 불가능합니다. 대신 `users.token_version`
(alembic `0003_add_user_token_version`)을 로그아웃 시 1 증가시키고, refresh_token 안에
발급 시점의 버전을 담아 재발급 요청마다 비교합니다 — 어긋나면 거부합니다
(`app/core/security.py`, `app/routers/auth.py`).

인증 없이 열려 있는 엔드포인트(signup/login/google/refresh/onboarding/password
forgot·reset/legal/shared)는 IP 기준 rate limit이 걸려 있습니다(`slowapi`,
`app/core/rate_limit.py`) — signup/login/google 10/분, refresh 20/분, onboarding 5/분,
password/forgot 5/분, password/reset 10/분, legal 60/분, `/shared/{token}` 300/시간.

**`/papers/{id}/revisions`와 `/papers/{id}/story`는 100회/시간으로 묶여 있습니다**
(2026-08-17에 30→100으로 상향). 이 둘만 외부 자원을 쓰기 때문입니다(OpenReview 2콜,
`/story`는 LLM까지). 인증을 걸지 않은 이유는 랜딩의 데모가 비로그인으로 `/story`를
부르기 때문이고, 캐시된 논문을 다시 읽는 것은 DB 조회 1번이라 비용이 없습니다 —
막아야 할 것은 **캐시에 없는 논문을 연달아 훑는 것**이라 시간당 상한이 맞습니다.
`/revisions/body-diff`는 캐시 미스 1건의 비용이 더 커서 **30회/시간**으로 더 좁게
잡혀 있습니다.

⚠️ **`refresh=true`만 로그인을 요구합니다.** 캐시를 우회하므로 같은 논문에 반복하면
시간당 상한을 무의미하게 만들고 호출마다 LLM이 돕니다. 비로그인 요청은 조용히 캐시를
주지 않고 **401로 거절**합니다 — 조용히 주면 호출자가 방금 다시 만든 결과를 받았다고
오해합니다. 저장소가 메모리라 워커를 여러 개로 늘리면
워커별로 따로 세므로, 그때는 Redis 저장소로 바꿔야 합니다. 백엔드 테스트
(`tests/app/`)는 반복 호출 때문에 이 제한을 꺼두고 돕니다 (conftest의 `_disable_rate_limit`).

## 8. AI팀 연동 방식

AI 파트는 **같은 프로세스에서 import**해서 씁니다 (별도 서비스 아님). 공개 계약은
함수 열 개이고, `paper_assistant/__init__.py`의 `__all__`이 그 목록입니다.

```python
from paper_assistant import (
    analyze, warmup, get_paper_detail, get_paper_reviews, get_paper_revisions,
    get_paper_revisions_with_body, get_paper_story, list_papers,
    extract_pdf_title_abstract, pdf_page_count)

report   = analyze(title, abstract, pdf_bytes=None, use_llm=None)  # -> Report
warmup()                                     # 기동 시 SPECTER2 선로드 (선택)
detail   = get_paper_detail(paper_id)        # -> PaperDetail | None    (DB만)
reviews  = get_paper_reviews(paper_id)       # -> list[ReviewDetail] | None
revs     = get_paper_revisions(paper_id)     # -> PaperRevisions | None (외부 API)
revs_body = get_paper_revisions_with_body(paper_id, refresh=False)
                                             # -> PaperRevisions | None (본문 diff 포함, 캐시)
story    = get_paper_story(paper_id, use_llm=None, refresh=False)
                                             # -> PaperStory | None (외부 API + LLM)
listing  = list_papers(venue=..., year=..., field=..., q=..., limit=, offset=)
pages    = pdf_page_count(pdf_bytes)         # 추출 전 페이지 상한 검사용 (싸다)
title, abstract = extract_pdf_title_abstract(pdf_bytes, use_llm=None)  # 업로드 경로용
```

무거운 의존성(torch 등)은 서버 기동이 아니라 **첫 호출 때** 로드되도록 전부 지연
import입니다 — `import paper_assistant` 자체는 가볍습니다.

⚠️ **이 목록 밖의 내부 모듈을 `app/`에서 import하지 마세요.** 예외는 타입을 위한
`paper_assistant.schemas`와 공유 설정인 `paper_assistant.config` 둘뿐이고,
`tests/meta/test_package_boundary.py`가 이를 강제합니다. 필요한 기능이 목록에 없으면
내부 모듈을 찌르지 말고 `__init__.py`에 공개 함수를 추가하세요 — LLM 인스턴스나 그래프
컴파일 같은 내부 결정이 백엔드로 새면, AI 파트를 고칠 때 `app/`이 함께 깨집니다.

- `get_paper_revisions()`만 **외부 네트워크(OpenReview API)** 를 탑니다. papers 테이블은
  openreview_id로 upsert해서 최신 버전만 남기 때문에 과거 버전이 DB에 없습니다.
  느리고 실패할 수 있으므로 사용자가 명시적으로 요청했을 때만 호출합니다.
  `supported=false`는 "수정이 없었다"가 아니라 "볼 수 없다"는 뜻이며(2023년 이전 학회는
  저자 수정 이력을 공개하지 않음), 이때 `message`를 그대로 사용자에게 보여주면 됩니다.

- `get_paper_story()`는 위 조회들을 **시간축으로 엮은** 것입니다. 세 부분(`journey` /
  `timeline` / `narrative`)이 서로 **독립적으로 실패**하도록 만들었습니다 — 외부 API가
  죽어도 `journey`(DB만 조회)는 나가고, LLM이 꺼져 있어도 `timeline`은 나갑니다. 한
  부분이 비었다고 전체를 실패로 처리하지 마세요.

  실측으로 확인한 한계 세 가지가 있고, 전부 `caveats`에 사용자용 문구로 들어갑니다:

  1. **리뷰 본문·점수는 최종 수정본입니다.** 리뷰어가 저자 응답 이후 리뷰를 고쳤어도
     우리가 가진 건 고쳐진 뒤의 내용뿐인데, 그걸 최초 게시 시각에 붙여 보여주게 됩니다.
     수정 전 점수는 복원할 수 없습니다 — 리뷰 노트의 edit 이력에서 첫 edit은 content가
     빈 채로 내려옵니다(표본 3건 전부). 그래서 **"6점 → 7점" 같은 표시를 만들면 안
     되고**, 수정이 있었다는 사실만 `review_update` 이벤트로 옵니다.
  2. **저자 수정으로 볼 수 있는 건 제목·초록·첨부파일까지입니다.** 본문은 PDF 안이라
     읽을 수 없으므로 "실험을 추가했다"는 화면에서 단정하면 안 됩니다. LLM 프롬프트에도
     같은 제약을 걸어 뒀습니다("~하겠다고 답변함"까지만 쓰게).
  3. **v2 학회인데도 수정 이력이 안 열리는 경우가 흔합니다** (무작위 14편 중 저자
     수정본이 보인 건 3편, ICLR 2024·NeurIPS는 대개 게재 확정본만). 이걸 "수정 없음"
     으로 그리지 마세요.

  반대로 **2023년 이전 학회도 리뷰·저자 응답 타임라인은 나옵니다** (ICLR 2022 실측:
  리뷰 4건 + 저자 응답 8건). `/revisions`가 `supported=false`로 아무것도 주지 못하던
  구간이라, 구 학회 논문은 `/story` 쪽이 훨씬 많은 것을 보여줍니다.

- `analyze()`는 **stateless**입니다 — DB에 아무것도 쓰지 않고 `Report`만 돌려줍니다.
  결과를 사용자·submission에 묶어 저장하는 건 `app/services/analysis.py`의 몫입니다.
- **동기 응답이 아닙니다.** 임베딩 모델 로드(첫 호출 수십 초) + 검색 + 집계가 들어가서
  실측 77초가 걸렸습니다. 그래서 POST가 `pending` 행만 만들고 202로 돌아온 뒤,
  `BackgroundTasks`가 실행하며 status를 `running` → `done`/`failed`로 옮깁니다.
- `Report`는 `report` JSONB에 통째로 저장합니다. AI 파트가 필드를 늘려도 마이그레이션이
  필요 없습니다. 스키마 정의는 `paper_assistant/schemas.py`에 있습니다.
- 비용: 기본은 **$0**입니다 (`PAPER_ASSISTANT_USE_LLM=0`, 규칙·통계 기반 스텁 종합).
  `1`로 켜면 Haiku(태깅)/Sonnet(종합)을 실제 호출하고, 그 사실이 `Report.used_llm`과
  응답의 `explanation_source`(`stub`/`llm`)에 남습니다. 설정값이 아니라 **실행 결과**를
  기록하므로 "켠 줄 알았는데 스텁이 나온" 경우를 구분할 수 있습니다.

### Report 주요 섹션

| 필드 | 내용 |
|---|---|
| `confidence` | 이 검색 결과를 믿어도 되는지 (strong/moderate/weak) |
| `selected_papers` | **화면의 주인공.** LLM이 고른 최대 5편 — 선정 이유, 리뷰 전문, AC 총평, 평점 |
| `similar_papers` | 검색 후보 50편 — rank(검색 순위), match_type. **근거 추적용이지 결과가 아님** |
| `summary_markdown` | 사람이 읽는 종합 요약. `[E1]`/`[M1]`은 아래 evidence를 가리킴 |
| `evidence` | 인용 가능한 **검색된 원문** — 리뷰 지적 문장(E*) + AC 메타리뷰(M*) |
| `citations` | 요약이 실제로 인용한 라벨. 지어낸 라벨은 제거되므로 링크는 유효하다 (원문이 그 주장을 뒷받침하는지까지는 미검증) |
| `used_llm` | 이 리포트가 실제 LLM 호출로 만들어졌는지 (근거 추적용) |
| `preferences` | 이 분석에 **실제로 적용된** 온보딩 선호 (`similarity_focus`, `recency_bias`). 온보딩 테이블의 현재 값이 아니라 이 실행이 쓴 값이라, 사용자가 마이페이지에서 답을 바꿔도 지난 분석의 기록은 그대로다 |

⚠️ **2026-08-06 개편으로 없어진 필드**: `review_patterns`, `venue_trends`,
`rating_context`, `resubmission_flows`. 통계 레이어를 통째로 걷어냈습니다 — 5편 위에서는
lift도 Fisher 검정도 무의미하기 때문입니다. 되살리려면
[추천_파이프라인_재설계.md](추천_파이프라인_재설계.md) 결정 #1을 먼저 읽으세요.

## 9. 브랜치 전략

- **main**: 항상 배포 가능한 안정 상태.
- **dev**: 실제 개발 브랜치. 기능 단위 작업은 dev에 커밋/푸시하고, main 반영은 합의 후 병합.

## 10. 남은 작업

- [x] **분석 대기 UX** — 여전히 프론트 폴링이지만, `review_predictions.progress`
      (`alembic 0014`, 2026-08-16)로 단계별 진행 상황을 응답에 실어 보내고, FE가
      `AnalysisProgress.jsx`로 표시합니다. 배포 환경은 `WARMUP_ON_STARTUP=1`로
      모델 로드를 기동 시점으로 옮겼습니다(D7, [RUNBOOK.md](../RUNBOOK.md) §4).
- [ ] **워커 분리 검토** — `BackgroundTasks`는 API 프로세스 안에서 돕니다. 동시 분석이
      늘면 API 응답이 느려지므로, 트래픽이 생기면 별도 워커로 빼야 합니다.
- [x] **프론트 연동** — [AICE-FE](https://github.com/AICE-GACHON/AICE-FE)(Vite + React)를
      연결했습니다. 온보딩 → 회원가입(온보딩 연결) → 로그인 → `/api/user/me`까지
      브라우저에서 CORS 포함 확인했고, 이로써 역할이 끝난 `demo/`는 삭제했습니다.
- [x] **파이프라인 2단계 개편** — PDF 전용 입력 + LLM 재정렬 + 통계 레이어 제거.
      2026-08-06 완료 (위 §1 표). 근거와 실측은
      [추천_파이프라인_재설계.md](추천_파이프라인_재설계.md).
- [x] **프론트에 6번 규칙 반영** — 예측형 문구를 결과 화면과 랜딩 카피에서 제거,
      유사도 점수 미표시, `weak` 경고 배너, `is_unsplit` 리뷰를 '리뷰 본문'으로 표시,
      선정이 비면 후보로 채우지 않음. 브라우저에서 확인했습니다.
- [ ] ⚠️ **후보 품질 올리기** — 실사용에서 LLM이 검색 33·34·49위를 골랐습니다(49위는
      경계에서 한 칸). **N은 50으로 고정**하고, 참고문헌 매칭 채널을 넣어 그 논문들이
      애초에 상위로 올라오게 합니다 (재설계 문서 §9.1).
- [ ] **"정말 비슷한가"의 자동 평가** — `eval_retrieval.py`가 재던 aspect 예측이
      목표에서 빠졌고 대체가 없습니다. `similar_paper_matches`에 후보와 선정이 함께
      쌓이므로, 실사용이 모이면 "LLM이 검색 어디쯤에서 고르는가"부터 SQL로 잴 수 있습니다.
- [x] **제목 추출 수정** — span을 잇는 기준을 공백+정규식에서 **bbox 좌표**로 바꿨습니다.
      `L ORA: LOW -RANK ADAPTATION OF LARGE LAN GUAGE MODELS` →
      `LORA: LOW-RANK ADAPTATION OF LARGE LANGUAGE MODELS`. 소문자 복원은 가능하지만
      검색 결과가 바뀌지 않아(top-10 일치 10/10) 만들지 않았습니다.
- [x] **스캔본 대응** — 텍스트 추출이 비거나 깨지면 앞 2페이지를 130dpi PNG로 렌더해
      Haiku 비전이 읽습니다(`pdf/extract.py`의 `_from_page_images`). 실호출 검증:
      스캔본으로 구운 LoRA 논문에서 제목·초록 1,388자를 복원, in=3,116/out=381 → **$0.005**.
      **실패했을 때만** 돌기 때문에 정상 업로드에는 비용이 붙지 않습니다.
- [x] **arXiv/S2 보강 실행** — 설계서 §20의 파이프라인을 2026-08-02에 전 단계
      실행했습니다(결과는 §25). alembic `0008`로 `papers.citation_count` 드리프트를
      먼저 복구해야 했습니다.
- [x] **보강 필드를 실제로 쓰기** — `citation_count`는 alembic `0010`의
      `citation_percentile`(같은 연도 내 백분위)로 환산돼 검색 랭킹에 들어갔습니다.
      소비처를 못 찾은 `final_venue`·`authors.s2_author_id`와 한 번도 적재하지 않은
      `citations`는 alembic `0012`에서 제거했습니다 — 채우는 비용(S2 호출)만 나가고
      읽는 쪽이 없는 상태를 유지하지 않기로 했습니다.
- [x] **코퍼스 중복 — 검색에서 접음** (설계서 §26.5). 실측 302쌍 중 301쌍이
      NeurIPS 2021이고, **두 행의 리뷰가 하나도 겹치지 않습니다**(쌍당 평균 7.8건).
      한쪽을 지우면 실제 리뷰 약 1,170건이 사라지므로 **DB는 건드리지 않고**
      검색에서만 하나로 접습니다(`_pick_without_duplicates`, 리뷰 많은 쪽을 남김).
- [ ] **중복의 근본 원인 확인** — 왜 NeurIPS 2021만, 왜 21.7%인지. OpenReview API로
      두 forum이 실제로 존재하는지 봐야 합니다. 저장을 건드리지 않았으므로 밝혀진 뒤에
      판단하면 됩니다.
- [ ] 코퍼스 DB 덤프 배포 경로 확정 (수 GB, git 불가).
