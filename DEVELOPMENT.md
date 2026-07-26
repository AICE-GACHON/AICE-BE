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

Python **3.13** 기준입니다. `psycopg2-binary`와 `openreview-py`는 제거했습니다
(각각 3.13 휠 부재 / 미사용).

## 3. 폴더 구조

```
AICE/
├── app/                      # 백엔드 (FastAPI)
│   ├── main.py                 # 앱 진입점 (미들웨어/에러 핸들러/라우터 등록)
│   ├── database.py             # DB 엔진/세션 + psycopg3 방언 변환
│   ├── core/
│   │   ├── config.py            # .env 기반 환경설정 (Settings)
│   │   ├── security.py          # 비밀번호 해시 + JWT 생성/검증
│   │   ├── deps.py              # get_current_user 등 Depends 함수
│   │   └── error_handlers.py    # 전역 예외 핸들러 (응답 포맷 통일)
│   ├── models/                 # SQLAlchemy 모델 — 서비스 테이블만
│   │   ├── user.py               # users
│   │   ├── submission.py         # submissions, similar_paper_matches
│   │   └── feedback.py           # review_predictions
│   ├── routers/
│   │   ├── auth.py               # 회원가입/로그인
│   │   ├── user.py               # 내 정보 조회
│   │   ├── paper.py              # 코퍼스 논문 상세 (AI 파트 위임)
│   │   ├── review.py             # 코퍼스 논문의 리뷰 목록 (AI 파트 위임)
│   │   ├── submission.py         # 내 논문 초안 업로드
│   │   └── feedback.py           # 분석 시작/조회 (핵심)
│   ├── schemas/                # Pydantic 요청/응답 스키마
│   └── services/
│       └── analysis.py         # ★ 백엔드와 AI 파트가 만나는 유일한 지점
├── paper_assistant/          # AI 파트 (공개 API: analyze, get_paper_detail)
│   ├── ingest/                 # OpenReview 수집 + 정규화 + arXiv/S2 보강
│   ├── embedding/              # SPECTER2
│   ├── retrieval/              # 하이브리드 검색 (벡터 + 전문검색 RRF)
│   ├── graph/                  # LangGraph 고정 DAG (분석 노드들)
│   ├── db/                     # psycopg3 커넥션 풀 + 적재
│   └── schemas.py              # Report 등 통합 계약 스키마
├── scripts/                  # 코퍼스 스키마(init_db.sql) + 수집/집계 배치
├── tests/                    # AI 파트 테스트 (135건)
├── demo/                     # 통합 계약 참고용 데모 (독립 실행, 삭제 가능)
├── alembic/versions/0001_initial_tables.py
├── docker-compose.yml        # pgvector Postgres (포트 5433)
└── requirements.txt
```

## 4. 데이터 모델

DB는 하나지만 **소유자가 둘로 나뉩니다.** 이 경계를 넘지 않는 것이 중요합니다.

```
[ 서비스 테이블 — alembic이 관리 ]        [ 논문 코퍼스 — scripts/init_db.sql이 관리 ]

users ──< submissions                     papers ──< reviews ──< review_points
              │                             │  └──< paper_authors >── authors
              └──< review_predictions       │  └──< submission_links (재투고 흐름)
                        │                   venue_stats, aspect_base_rates, citations
                        └──< similar_paper_matches ┄┄(paper_id, FK 없음)┄┄> papers
```

### 서비스 테이블
- **users**: 회원. 이메일/비밀번호 기반 인증. `token_version`은 refresh_token 폐기용
  버전 카운터로, 로그아웃 시 증가시켜 그 이전에 발급된 refresh_token을 전부 무효화합니다
  (`alembic/versions/0002_add_user_token_version.py`).
- **submissions**: 사용자가 올린 내 논문 초안. 임베딩은 저장하지 않고 분석할 때마다 계산합니다.
- **review_predictions**: 분석 1회분. 백그라운드 작업의 상태(`pending/running/done/failed`)이자
  결과 저장소로, 분석 결과 전체가 `report` JSONB에 들어갑니다.
- **similar_paper_matches**: 그 분석이 근거로 삼은 유사 논문 목록 (`rank`, `match_type`).

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
- `alembic upgrade head`는 서비스 테이블 4개만 만듭니다.

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
| Auth | `POST /api/auth/signup` | 회원가입 | - |
| Auth | `POST /api/auth/login` | 로그인, access_token + refresh_token 발급 | - |
| Auth | `POST /api/auth/refresh` | refresh_token으로 access_token 재발급 (refresh_token도 회전) | - |
| Auth | `POST /api/auth/logout` | 로그아웃 (User.token_version 증가 → 이전 refresh_token 전부 무효화) | 필요 |
| User | `GET /api/user/me` | 내 정보 조회 | 필요 |
| User | `PATCH /api/user/me` | 내 정보 수정 (nickname) | 필요 |
| User | `DELETE /api/user/me` | 회원 탈퇴 (submissions 이하 CASCADE 삭제) | 필요 |
| Submission | `POST /api/submissions` | 내 논문 초안 업로드 | 필요 |
| Submission | `GET /api/submissions` | 내 초안 목록 조회 | 필요 |
| Submission | `GET /api/submissions/{id}` | 내 초안 상세 조회 | 필요 |
| Submission | `DELETE /api/submissions/{id}` | 초안 삭제 (review_predictions 이하 CASCADE 삭제) | 필요 |
| Feedback | `POST /api/submissions/{id}/analysis` | 분석 시작 → **202**, status=pending | 필요 |
| Feedback | `GET /api/submissions/{id}/analysis` | 분석 상태/결과 조회 (폴링) | 필요 |
| Paper | `GET /api/papers` | 코퍼스 논문 목록 (venue/year/field/q 필터, limit/offset 페이지네이션) | - |
| Paper | `GET /api/papers/{paper_id}` | 코퍼스 논문 상세 (초록·리뷰 전문·지적 항목) | - |
| Paper | `GET /api/papers/{paper_id}/revisions` | 저자 수정 이력 (**외부 API 실시간 조회**) | - |
| Review | `GET /api/reviews?paper_id=` | 특정 논문의 리뷰 목록 | - |

⚠️ `paper_id`는 UUID가 아니라 **BIGINT**입니다 (코퍼스가 BIGSERIAL). 분석 결과의
`similar_papers[].paper_id`를 그대로 넘기면 됩니다.

기존 `POST /api/feedback/predictions`(501 반환)는 분석 시작/조회 두 개로 대체돼 삭제됐습니다.

refresh_token은 JWT라 상태가 없어 개별 폐기가 불가능합니다. 대신 `users.token_version`
(alembic `0002_add_user_token_version`)을 로그아웃 시 1 증가시키고, refresh_token 안에
발급 시점의 버전을 담아 재발급 요청마다 비교합니다 — 어긋나면 거부합니다
(`app/core/security.py`, `app/routers/auth.py`).

## 8. AI팀 연동 방식

AI 파트는 **같은 프로세스에서 import**해서 씁니다 (별도 서비스 아님). 공개 계약은 함수 세 개입니다.

```python
from paper_assistant import analyze, get_paper_detail, get_paper_revisions

report   = analyze(title, abstract, pdf_bytes=None, use_llm=None)  # -> Report
detail   = get_paper_detail(paper_id)        # -> PaperDetail | None    (DB만)
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
  `1`로 켜면 Haiku(추출)/Sonnet(종합)을 실제 호출하고, 그 사실이 응답의
  `explanation_source`(`stub`/`llm`)에 남습니다.

### Report 주요 섹션

| 필드 | 내용 |
|---|---|
| `confidence` | 이 검색 결과를 믿어도 되는지 (strong/moderate/weak) |
| `similar_papers` | 유사 논문 20편 — rank, match_type, 리뷰 점수(상대값) |
| `review_patterns` | 반복 등장 지적 — lift, p값, 이 지적을 받은/안 받은 논문의 당락 대조 |
| `venue_trends` | 학회별 게재 경향 (`is_coverage_biased` 확인 필수) |
| `rating_context` | 이웃 논문 점수 분포와 당락 경계 추정 |
| `resubmission_flows` | A학회 reject → B학회 accept 흐름 |
| `summary_markdown` | 사람이 읽는 종합 요약 |

## 9. 브랜치 전략

- **main**: 항상 배포 가능한 안정 상태.
- **dev**: 실제 개발 브랜치. 기능 단위 작업은 dev에 커밋/푸시하고, main 반영은 합의 후 병합.

## 10. 남은 작업

- [ ] **PDF 업로드** — `analyze(pdf_bytes=...)`는 이미 PDF에서 제목/초록을 추출할 수 있는데,
      `POST /api/submissions`가 JSON만 받습니다. multipart 업로드 경로를 추가해야 합니다.
- [ ] **분석 대기 UX** — 현재는 프론트 폴링입니다. 첫 요청이 특히 느리므로(모델 로드)
      서버 기동 시 워밍업을 넣을지 결정 필요 (`demo/server.py`의 startup 훅이 참고 구현).
- [ ] **워커 분리 검토** — `BackgroundTasks`는 API 프로세스 안에서 돕니다. 동시 분석이
      늘면 API 응답이 느려지므로, 트래픽이 생기면 별도 워커로 빼야 합니다.
- [ ] **프론트 연동 테스트** — CORS/인증/응답 포맷 + 위 6번 4가지 규칙 반영 확인.
- [ ] 코퍼스 DB 덤프 배포 경로 확정 (수 GB, git 불가).
