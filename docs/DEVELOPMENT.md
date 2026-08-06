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
│   │   └── errors.py            # 전역 예외 핸들러 (응답 포맷 통일)
│   ├── models/                 # SQLAlchemy 모델 — 서비스 테이블만
│   │   ├── user.py               # users
│   │   ├── submission.py         # submissions
│   │   ├── analysis.py           # review_predictions, similar_paper_matches
│   │   └── onboarding.py         # onboarding_profiles
│   ├── routers/
│   │   ├── auth.py               # 회원가입/로그인/구글 로그인/refresh/logout
│   │   ├── user.py               # 내 정보 조회/수정/탈퇴, 온보딩 조회
│   │   ├── submissions.py        # 내 논문 업로드(PDF)·조회·삭제 + 분석 시작/조회 (핵심)
│   │   ├── corpus.py             # 코퍼스 논문 목록/상세/리뷰/수정 이력 (AI 파트 위임)
│   │   └── onboarding.py         # 회원가입 전 익명 온보딩 답변 저장
│   ├── schemas/                # Pydantic 요청/응답 스키마
│   │   ├── common.py             # ApiResponse[T]
│   │   ├── auth.py / submission.py / analysis.py
│   │   └── corpus.py             # AI 파트 스키마 재수출 (중복 정의 금지)
│   └── services/
│       └── analysis.py         # ★ 백엔드와 AI 파트가 만나는 유일한 지점
├── paper_assistant/          # AI 파트 (공개 API 7개)
│   ├── config.py               # ★ 공유 환경설정의 단일 소스
│   ├── schemas.py              # Report 등 통합 계약 스키마
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
│   ├── app/                    # 백엔드 (인증·소유권·분석 상태 전이)
│   ├── paper_assistant/        # AI 파트
│   └── test_backend_auth.py    # 백엔드 인증/온보딩 연동 라우터 테스트
├── docs/                     # 설계서·팀 공유 문서·개발 문서
├── alembic/versions/         # 0001 초기 테이블 … 0011 PDF 저장 + LLM 선정 (아래 §4 참고)
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
  │           │                             │  └──< paper_authors >── authors
  │           └──< review_predictions       │  └──< submission_links (재투고 흐름)
  │                     │                   venue_stats, aspect_base_rates, citations
  │                     └──< similar_paper_matches ┄┄(paper_id, FK 없음)┄┄> papers
  └──< onboarding_profiles (1:1, user_id nullable — 회원가입 전엔 주인 없음)
```

### 서비스 테이블
- **users**: 회원. 이메일/비밀번호 또는 구글(`google_sub`) 인증. `openreview_id`는
  가입 경로와 무관하게 필수이며 **unique**입니다(서비스가 OpenReview 코퍼스 기반이라
  신원 값으로 사용 — 두 계정이 같은 OpenReview 계정을 자처할 수 없게 잠갔습니다,
  `alembic/versions/0006_unique_openreview_id.py`). `password_hash`는 구글 전용
  계정이 있어 nullable입니다 (`alembic/versions/0004_add_google_and_openreview_id.py`).
  `token_version`은 refresh_token 폐기용 버전 카운터로, 로그아웃 시 증가시켜 그
  이전에 발급된 refresh_token을 전부 무효화합니다
  (`alembic/versions/0003_add_user_token_version.py`).
- **submissions**: 사용자가 올린 내 논문. **PDF 원본을 `pdf_bytes`에 저장합니다** —
  분석이 BackgroundTasks라 응답 이후에 도는데, 2단계 LLM이 본문·참고문헌을 봐야 하기
  때문입니다. ⚠️ `deferred=True`로 매핑돼 있습니다(최대 20MB 블롭이라, 아니면 목록
  조회가 행마다 이걸 끌어옵니다). 임베딩은 저장하지 않고 분석할 때마다 계산합니다.
- **review_predictions**: 분석 1회분. 백그라운드 작업의 상태(`pending/running/done/failed`)이자
  결과 저장소로, 분석 결과 전체가 `report` JSONB에 들어갑니다.
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
  (기본 30일 지난 행 삭제, `--dry-run`으로 미리 확인 가능).

### 논문 코퍼스 (AI 파트 소유, 43,515편)
- **papers / reviews / review_points**: ICLR 2020–2025 + NeurIPS 2021–2024에서 수집한
  논문·리뷰·개별 지적 항목 (리뷰 168,217건, 지적항목 119만 건).
- **venue_stats / aspect_base_rates**: 학회별 점수 기준선과 코퍼스 전체 지적 비율.
  ⚠️ **분석 경로는 더 이상 읽지 않습니다**(통계 레이어 제거). `venue_stats`는 `/story`가,
  `aspect_base_rates`는 `scripts/eval_retrieval.py`가 쓰므로 남겨 뒀습니다.
- **submission_links**: 같은 논문의 재투고 추적 (ICLR reject → NeurIPS accept). 747건.
- **arXiv/S2 보강 필드** (`papers.arxiv_id`·`s2_paper_id`·`citation_count`·`final_venue`,
  `authors.s2_author_id`): 2026-08-02에 채웠습니다(설계서 §25). **코퍼스 전체를 덮지
  않습니다** — `s2_paper_id` 기준 채택 논문 98.1% / 탈락 논문 38.2%이고, 전체로는
  69.5%입니다. **`citation_count`와 `final_venue`는 아직 검색·분석이 읽지 않습니다.**
  `citations`(인용 엣지)는 0행입니다.

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

## 5. 로컬 실행 방법

[README.md](README.md)의 "로컬 실행"을 따르세요. 기존과 달라진 점만 요약하면:

- DB는 로컬 PostgreSQL이 아니라 `docker compose up -d`로 띄우는 **pgvector 인스턴스(5433)** 입니다.
- 논문 코퍼스는 git에 없습니다. `scripts/restore_db.sh`로 DB 덤프를 복원해야 합니다.
- `alembic upgrade head`는 서비스 테이블 5개(`users`, `submissions`,
  `review_predictions`, `similar_paper_matches`, `onboarding_profiles`)만 만듭니다.

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

모든 응답은 `{ "success": bool, "data": ..., "error": { "code", "message" } | null }` 형태로
통일돼 있습니다 (`app/schemas/common.py`의 `ApiResponse[T]`).

| 도메인 | 메서드/경로 | 설명 | 인증 |
|---|---|---|---|
| Auth | `POST /api/auth/signup` | 회원가입 (openreview_id 필수, onboarding_id로 온보딩 답변 연결 선택) | - |
| Auth | `POST /api/auth/login` | 로그인, access_token + refresh_token 발급 | - |
| Auth | `POST /api/auth/google` | 구글 로그인/연동 (id_token 검증, 신규 가입 시 openreview_id 필수) | - |
| Auth | `POST /api/auth/refresh` | refresh_token으로 access_token 재발급 (refresh_token도 회전) | - |
| Auth | `POST /api/auth/logout` | 로그아웃 (User.token_version 증가 → 이전 refresh_token 전부 무효화) | 필요 |
| User | `GET /api/user/me` | 내 정보 조회 | 필요 |
| User | `PATCH /api/user/me` | 내 정보 수정 (nickname, openreview_id) | 필요 |
| User | `DELETE /api/user/me` | 회원 탈퇴 (submissions 이하 CASCADE 삭제) | 필요 |
| User | `GET /api/user/me/onboarding` | 내 온보딩 답변 조회 (마이페이지) | 필요 |
| Submission | `POST /api/submissions/pdf` | 내 논문 업로드 — **유일한 입력 경로**. title/abstract가 비면 PDF에서 추출, 응답에 `page_count`. 추출 실패·20MB 초과·60p 초과는 422 | 필요 |
| Submission | `GET /api/submissions` | 내 초안 목록 (최신순, 본문 제외) | 필요 |
| Submission | `GET /api/submissions/{id}` | 초안 상세 | 필요 |
| Submission | `DELETE /api/submissions/{id}` | 초안 삭제 (분석 결과도 함께) → **204** | 필요 |
| Submission | `POST /api/submissions/{id}/analysis` | 분석 시작 → **202**, status=pending | 필요 |
| Submission | `GET /api/submissions/{id}/analysis` | 분석 상태/결과 조회 (폴링) | 필요 |
| Corpus | `GET /api/papers` | 코퍼스 논문 목록 (venue/year/field/q 필터, limit/offset 페이지네이션) | - |
| Corpus | `GET /api/papers/{paper_id}` | 코퍼스 논문 상세 (초록·리뷰 전문·지적 항목) | - |
| Corpus | `GET /api/papers/{paper_id}/reviews` | 그 논문의 리뷰 목록 | - |
| Corpus | `GET /api/papers/{paper_id}/revisions` | 저자 수정 이력 (**외부 API 실시간 조회**) | - |
| Corpus | `GET /api/papers/{paper_id}/story` | 심사 서사 — 재투고 궤적 + 리뷰·응답·수정 타임라인 + 요약 (**외부 API 실시간 조회 + LLM**, `paper_stories`에 캐시) | - |
| Onboarding | `POST /api/onboarding` | 회원가입 전 익명 온보딩 답변 저장 | - |

⚠️ `paper_id`는 UUID가 아니라 **BIGINT**입니다 (코퍼스가 BIGSERIAL). 분석 결과의
`similar_papers[].paper_id`를 그대로 넘기면 됩니다.

남의 초안에 접근하면 403이 아니라 **404**입니다 — 존재 여부 자체를 알리지 않습니다.

⚠️ 리뷰 목록 경로가 `GET /api/reviews?paper_id=`에서 `GET /api/papers/{paper_id}/reviews`로
바뀌었습니다 (같은 리소스의 하위 경로가 맞고, 예전 경로는 상세 조회를 통째로 돌린 뒤
리뷰만 꺼내 쓰느라 불필요한 쿼리 3개를 더 날렸습니다).

기존 `POST /api/feedback/predictions`(501 반환)는 분석 시작/조회 두 개로 대체돼 삭제됐습니다.

refresh_token은 JWT라 상태가 없어 개별 폐기가 불가능합니다. 대신 `users.token_version`
(alembic `0003_add_user_token_version`)을 로그아웃 시 1 증가시키고, refresh_token 안에
발급 시점의 버전을 담아 재발급 요청마다 비교합니다 — 어긋나면 거부합니다
(`app/core/security.py`, `app/routers/auth.py`).

인증 없이 열려 있는 엔드포인트(signup/login/google/refresh/onboarding)는 IP 기준
rate limit이 걸려 있습니다(`slowapi`, `app/core/rate_limit.py`) — signup/login/google
10/분, refresh 20/분, onboarding 5/분. 저장소가 메모리라 워커를 여러 개로 늘리면
워커별로 따로 세므로, 그때는 Redis 저장소로 바꿔야 합니다. 백엔드 테스트
(`tests/test_backend_auth.py`)는 반복 호출 때문에 이 제한을 꺼두고 돕니다.

## 8. AI팀 연동 방식

AI 파트는 **같은 프로세스에서 import**해서 씁니다 (별도 서비스 아님). 공개 계약은
함수 일곱 개이고, `paper_assistant/__init__.py`의 `__all__`이 그 목록입니다.

```python
from paper_assistant import (
    analyze, get_paper_detail, get_paper_reviews, get_paper_revisions,
    get_paper_story, list_papers, extract_pdf_title_abstract)

report   = analyze(title, abstract, pdf_bytes=None, use_llm=None)  # -> Report
detail   = get_paper_detail(paper_id)        # -> PaperDetail | None    (DB만)
reviews  = get_paper_reviews(paper_id)       # -> list[ReviewDetail] | None
revs     = get_paper_revisions(paper_id)     # -> PaperRevisions | None (외부 API)
story    = get_paper_story(paper_id, use_llm=None, refresh=False)
                                             # -> PaperStory | None (외부 API + LLM)
listing  = list_papers(venue=..., year=..., field=..., q=..., limit=, offset=)
title, abstract = extract_pdf_title_abstract(pdf_bytes)   # PDF 업로드 경로용
```

무거운 의존성(torch 등)은 서버 기동이 아니라 **첫 호출 때** 로드되도록 전부 지연
import입니다 — `import paper_assistant` 자체는 가볍습니다.

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

⚠️ **2026-08-06 개편으로 없어진 필드**: `review_patterns`, `venue_trends`,
`rating_context`, `resubmission_flows`. 통계 레이어를 통째로 걷어냈습니다 — 5편 위에서는
lift도 Fisher 검정도 무의미하기 때문입니다. 되살리려면
[추천_파이프라인_재설계.md](추천_파이프라인_재설계.md) 결정 #1을 먼저 읽으세요.

## 9. 브랜치 전략

- **main**: 항상 배포 가능한 안정 상태.
- **dev**: 실제 개발 브랜치. 기능 단위 작업은 dev에 커밋/푸시하고, main 반영은 합의 후 병합.

## 10. 남은 작업

- [ ] **분석 대기 UX** — 현재는 프론트 폴링입니다. 첫 요청이 특히 느리므로(모델 로드)
      배포 환경에서는 `.env`에 `WARMUP_ON_STARTUP=1`을 켜서 기동 시점으로 옮기세요
      (기본은 off — 로컬 개발에서 매 기동이 수십 초 느려집니다).
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
- [ ] ⚠️ **후보 50편이 충분한지 확인** — 실호출에서 LLM이 검색 42·47위를 골랐습니다.
      47위는 경계에서 3칸입니다. N=50/75/100으로 선정 안정성을 비교해야 합니다
      (재설계 문서 §8a, 약 $20 + 논문 PDF 30편).
- [ ] **"정말 비슷한가"의 자동 평가** — `eval_retrieval.py`가 재던 aspect 예측이
      목표에서 빠졌고 대체가 없습니다. `similar_paper_matches`에 후보와 선정이 함께
      쌓이므로, 실사용이 모이면 "LLM이 검색 어디쯤에서 고르는가"부터 SQL로 잴 수 있습니다.
- [ ] **제목 추출 개선** — 전부 대문자 제목에서 드롭캡 복원 정규식이 오작동합니다
      (`L ORA: LOW -RANK ...`). 임베딩 품질에 영향을 주며, 스캔본 대응(Haiku 비전으로
      1페이지에서 복원)과 함께 보면 좋습니다.
- [x] **arXiv/S2 보강 실행** — 설계서 §20의 파이프라인을 2026-08-02에 전 단계
      실행했습니다(결과는 §25). alembic `0008`로 `papers.citation_count` 드리프트를
      먼저 복구해야 했습니다.
- [ ] **보강 필드를 실제로 쓰기** — `citation_count`·`final_venue`가 채워졌지만 읽는
      코드가 없습니다. 검색 랭킹 보정이나 논문 상세의 최종 게재처 표시 등 소비처를
      정하는 것이 먼저이고, `--citations`(인용 엣지 적재)는 그 뒤에 돌리면 됩니다.
- [ ] **코퍼스 중복 행 228쌍 정리** — 같은 논문이 같은 venue+year에 두 번 적재돼
      있어 검색 결과에 중복 노출될 수 있습니다 (설계서 §25.5).
- [ ] 코퍼스 DB 덤프 배포 경로 확정 (수 GB, git 불가).
