# DEVELOPMENT

## 1. 프로젝트 개요

**AICE**는 ML/AI 논문을 위한 리서치 어시스턴트입니다. 백엔드(FastAPI)와 AI 분석
파이프라인이 **한 저장소, 한 프로세스, 한 DB**로 통합돼 있습니다.

사용자가 자신의 논문 초안을 올리면, 비슷한 기존 논문을 찾아 그 논문들이 실제로 받았던
리뷰를 분석해서 "이 연구가 어떤 지적을 받을지, 어느 학회에서 어떤 평가를 받았는지"를
근거와 함께 제시합니다. 예상 결과가 정답처럼 보이지 않도록, 항상 어떤 유사 논문·리뷰를
근거로 삼았는지와 **그 결과를 믿어도 되는지(신뢰도)** 를 함께 노출하는 것이 설계 원칙입니다.

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
│   │   ├── submissions.py        # 내 초안 업로드(JSON/PDF)·조회·삭제 + 분석 시작/조회 (핵심)
│   │   ├── corpus.py             # 코퍼스 논문 목록/상세/리뷰/수정 이력 (AI 파트 위임)
│   │   └── onboarding.py         # 회원가입 전 익명 온보딩 답변 저장
│   ├── schemas/                # Pydantic 요청/응답 스키마
│   │   ├── common.py             # ApiResponse[T]
│   │   ├── auth.py / submission.py / analysis.py
│   │   └── corpus.py             # AI 파트 스키마 재수출 (중복 정의 금지)
│   └── services/
│       └── analysis.py         # ★ 백엔드와 AI 파트가 만나는 유일한 지점
├── paper_assistant/          # AI 파트 (공개 API 4개)
│   ├── config.py               # ★ 공유 환경설정의 단일 소스
│   ├── schemas.py              # Report 등 통합 계약 스키마
│   ├── query/                  # 조회 전용 (detail, revisions)
│   ├── graph/                  # LangGraph 고정 DAG (분석 노드들)
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
├── demo/                     # 임시 프론트 (프론트 연동 전까지 유지, 독립 실행)
│   ├── server.py               # paper_assistant만 호출 — 인증/DB 쓰기 없음
│   └── static/index.html       # 단일 페이지 (폼 + 결과 렌더링)
├── docs/                     # 설계서·팀 공유 문서·개발 문서
├── alembic/versions/         # 0001 초기 테이블 … 0006 openreview_id unique (아래 §4 참고)
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
- **submissions**: 사용자가 올린 내 논문 초안. 임베딩은 저장하지 않고 분석할 때마다 계산합니다.
- **review_predictions**: 분석 1회분. 백그라운드 작업의 상태(`pending/running/done/failed`)이자
  결과 저장소로, 분석 결과 전체가 `report` JSONB에 들어갑니다.
- **similar_paper_matches**: 그 분석이 근거로 삼은 유사 논문 목록 (`rank`, `match_type`).
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
  "이 지적이 이 주제에서 특별히 두드러지는가"를 판단하는 분모입니다.
- **submission_links**: 같은 논문의 재투고 추적 (ICLR reject → NeurIPS accept).

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

## 6. 프론트/백엔드가 반드시 지켜야 할 4가지

AI 파트가 실측으로 확인한 함정입니다. 수치와 근거는 [AI_파트_팀_공유.md](AI_파트_팀_공유.md) §4에 있습니다.

1. **유사도 점수는 없습니다.** 검색 상위 20편의 코사인 유사도 폭이 0.013이라 1위와 20위가
   사실상 같은 값입니다. "유사도 92%" 같은 UI를 만들면 안 되고, `rank`와
   `match_type`(both/semantic/lexical)으로 표시합니다.
2. **`confidence.level`이 `weak`이면 경고 배너가 필수**입니다. 이게 없으면 요리 레시피를
   넣어도 ML 논문 20편을 자신 있게 내놓습니다.
3. **리뷰 지적은 빈도순이 아니라 `is_distinctive` 기준**으로 강조합니다. 코퍼스 전체의
   78.8%가 baselines 지적을 받으므로 "20편 중 17편"은 정보량이 0입니다.
4. **`is_coverage_biased`가 true인 학회는 채택률 절대 수치를 노출하지 않습니다.**
   NeurIPS는 코퍼스의 95%가 accept로 보이지만 실제 채택률은 ~25%입니다.

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
| Submission | `POST /api/submissions` | 내 논문 초안 업로드 (JSON) | 필요 |
| Submission | `POST /api/submissions/pdf` | 내 논문 초안 업로드 (PDF, title/abstract 비면 추출) | 필요 |
| Submission | `GET /api/submissions` | 내 초안 목록 (최신순, 본문 제외) | 필요 |
| Submission | `GET /api/submissions/{id}` | 초안 상세 | 필요 |
| Submission | `DELETE /api/submissions/{id}` | 초안 삭제 (분석 결과도 함께) → **204** | 필요 |
| Submission | `POST /api/submissions/{id}/analysis` | 분석 시작 → **202**, status=pending | 필요 |
| Submission | `GET /api/submissions/{id}/analysis` | 분석 상태/결과 조회 (폴링) | 필요 |
| Corpus | `GET /api/papers` | 코퍼스 논문 목록 (venue/year/field/q 필터, limit/offset 페이지네이션) | - |
| Corpus | `GET /api/papers/{paper_id}` | 코퍼스 논문 상세 (초록·리뷰 전문·지적 항목) | - |
| Corpus | `GET /api/papers/{paper_id}/reviews` | 그 논문의 리뷰 목록 | - |
| Corpus | `GET /api/papers/{paper_id}/revisions` | 저자 수정 이력 (**외부 API 실시간 조회**) | - |
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

AI 파트는 **같은 프로세스에서 import**해서 씁니다 (별도 서비스 아님). 공개 계약은 함수 네 개입니다.

```python
from paper_assistant import (
    analyze, get_paper_detail, get_paper_reviews, get_paper_revisions)

report   = analyze(title, abstract, pdf_bytes=None, use_llm=None)  # -> Report
detail   = get_paper_detail(paper_id)        # -> PaperDetail | None    (DB만)
reviews  = get_paper_reviews(paper_id)       # -> list[ReviewDetail] | None
revs     = get_paper_revisions(paper_id)     # -> PaperRevisions | None (외부 API)
```

- `get_paper_revisions()`만 **외부 네트워크(OpenReview API)** 를 탑니다. papers 테이블은
  openreview_id로 upsert해서 최신 버전만 남기 때문에 과거 버전이 DB에 없습니다.
  느리고 실패할 수 있으므로 사용자가 명시적으로 요청했을 때만 호출합니다.
  `supported=false`는 "수정이 없었다"가 아니라 "볼 수 없다"는 뜻이며(2023년 이전 학회는
  저자 수정 이력을 공개하지 않음), 이때 `message`를 그대로 사용자에게 보여주면 됩니다.

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
| `similar_papers` | 유사 논문 20편 — rank, match_type, 리뷰 점수(상대값) |
| `review_patterns` | 반복 등장 지적 — lift, p값, 이 지적을 받은/안 받은 논문의 당락 대조 |
| `venue_trends` | 학회별 게재 경향 (`is_coverage_biased` 확인 필수) |
| `rating_context` | 이웃 논문 점수 분포와 당락 경계 추정 |
| `resubmission_flows` | A학회 reject → B학회 accept 흐름 |
| `summary_markdown` | 사람이 읽는 종합 요약. `[E1]`/`[M1]`은 아래 evidence를 가리킴 |
| `evidence` | 인용 가능한 **검색된 원문** — 리뷰 지적 문장(E*) + AC 메타리뷰(M*) |
| `citations` | 요약이 실제로 인용한 라벨. 지어낸 라벨은 제거되므로 링크는 유효하다 (원문이 그 주장을 뒷받침하는지까지는 미검증) |
| `used_llm` | 이 리포트가 실제 LLM 호출로 만들어졌는지 (근거 추적용) |

## 9. 브랜치 전략

- **main**: 항상 배포 가능한 안정 상태.
- **dev**: 실제 개발 브랜치. 기능 단위 작업은 dev에 커밋/푸시하고, main 반영은 합의 후 병합.

## 10. 남은 작업

- [ ] **분석 대기 UX** — 현재는 프론트 폴링입니다. 첫 요청이 특히 느리므로(모델 로드)
      배포 환경에서는 `.env`에 `WARMUP_ON_STARTUP=1`을 켜서 기동 시점으로 옮기세요
      (기본은 off — 로컬 개발에서 매 기동이 수십 초 느려집니다).
- [ ] **워커 분리 검토** — `BackgroundTasks`는 API 프로세스 안에서 돕니다. 동시 분석이
      늘면 API 응답이 느려지므로, 트래픽이 생기면 별도 워커로 빼야 합니다.
- [ ] **프론트 연동 테스트** — CORS/인증/응답 포맷 + 위 6번 4가지 규칙 반영 확인.
      연동이 끝나면 `demo/`를 삭제한다 (그때까지는 결과를 볼 수 있는 유일한 화면이다).
- [ ] 코퍼스 DB 덤프 배포 경로 확정 (수 GB, git 불가).
