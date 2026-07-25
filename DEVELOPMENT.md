# DEVELOPMENT

## 1. 프로젝트 개요

**AICE-BE**는 ML/AI 논문을 위한 리서치 어시스턴트 백엔드입니다 (FastAPI + PostgreSQL).

사용자가 자신의 논문(초안)을 올리면, 비슷한 기존 논문을 찾아서 그 논문이 받았던 리뷰와
리뷰 이후 어떻게 수정됐는지 보여주고, 이를 바탕으로 예상 리뷰 포인트와 수정 방향을
"근거와 함께" 제시하는 것이 핵심 기능입니다. 예상 결과가 정답처럼 보이지 않도록,
항상 어떤 유사 논문/리뷰를 근거로 삼았는지 함께 노출하는 것을 설계 원칙으로 삼고 있습니다.

## 2. 기술 스택

| 영역 | 사용 기술 |
|---|---|
| 웹 프레임워크 | FastAPI 0.115 |
| ASGI 서버 | Uvicorn |
| ORM | SQLAlchemy 2.0 |
| 마이그레이션 | Alembic |
| DB | PostgreSQL (psycopg2-binary) |
| 데이터 검증 | Pydantic v2 (+ pydantic-settings, email-validator) |
| 인증 | python-jose(JWT) + passlib[bcrypt] |
| 논문 데이터 수집 | openreview-py |

## 3. 폴더 구조

```
AICE-BE/
├── app/
│   ├── main.py             # FastAPI 앱 진입점 (미들웨어/에러 핸들러/라우터 등록)
│   ├── database.py         # DB 엔진/세션(get_db) 설정
│   ├── core/
│   │   ├── config.py        # .env 기반 환경설정 (Settings)
│   │   ├── security.py      # 비밀번호 해시 + JWT 생성/검증
│   │   ├── deps.py          # get_current_user 등 FastAPI Depends 함수
│   │   └── error_handlers.py# 전역 예외 핸들러 (응답 포맷 통일)
│   ├── models/              # SQLAlchemy 모델 (테이블 정의, 도메인별 분리)
│   │   ├── user.py            # users
│   │   ├── paper.py           # papers
│   │   ├── review.py          # reviews, revisions
│   │   ├── submission.py      # submissions, similar_paper_matches
│   │   └── feedback.py        # review_predictions
│   ├── routers/              # API 엔드포인트 (도메인별 분리)
│   │   ├── auth.py             # 회원가입/로그인
│   │   ├── user.py             # 내 정보 조회
│   │   ├── paper.py            # 기존 논문 코퍼스 조회
│   │   ├── review.py           # 기존 논문의 리뷰 조회
│   │   ├── submission.py       # 내 논문 초안 업로드
│   │   └── feedback.py         # 예상 리뷰/수정 제안 (뼈대)
│   └── schemas/              # Pydantic 요청/응답 스키마
│       ├── common.py           # ApiResponse[T], ErrorDetail 등 공통 스키마
│       ├── auth.py
│       ├── submission.py
│       ├── paper.py
│       ├── review.py
│       └── feedback.py
├── alembic/                 # DB 마이그레이션
│   └── versions/
│       └── 0001_initial_tables.py
├── requirements.txt
├── .env.example
└── .gitignore
```

## 4. 데이터 모델

```
users ──< submissions ──< similar_paper_matches >── papers ──< reviews
                │                                       └──< revisions
                └──< review_predictions
```

- **users**: 회원. 이메일/비밀번호 기반 인증만 사용합니다.
- **papers**: OpenReview API로 수집한, 이미 심사가 끝난 기존 논문들 (유사 논문 검색의 코퍼스).
- **reviews**: papers가 실제로 받았던 리뷰 (논문 하나에 여러 개 가능).
- **revisions**: papers가 리뷰를 받은 뒤 버전별로 어떻게 수정됐는지 기록.
- **submissions**: 사용자가 올린 "내 논문 초안". 아직 리뷰를 받지 않았다는 점이 papers와 다릅니다.
- **similar_paper_matches**: submission과 비슷한 papers를 검색한 결과 (유사도 점수 포함).
- **review_predictions**: 핵심 산출물. "예상 리뷰 포인트 + 수정 제안"을 담되, `based_on_matches`에
  어떤 similar_paper_matches를 근거로 삼았는지 같이 저장합니다 (판단 근거를 항상 보여주기 위한
  의도적 설계).

## 5. 로컬 실행 방법

```bash
# 1. 가상환경 생성
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # Mac/Linux

# 2. 패키지 설치
pip install -r requirements.txt

# 3. 환경변수 파일 생성
cp .env.example .env
# .env에 본인 PostgreSQL 접속 정보(DATABASE_URL), JWT_SECRET_KEY 등을 채운다

# 4. PostgreSQL에 빈 데이터베이스 미리 생성

# 5. 마이그레이션 실행 (테이블 생성)
alembic upgrade head

# 6. 서버 실행
uvicorn app.main:app --reload
```

- 헬스체크: http://localhost:8000
- API 문서(Swagger): http://localhost:8000/docs

## 6. 브랜치 전략

- **main**: 항상 배포 가능한 안정 상태를 유지하는 브랜치.
- **dev**: 실제 개발이 이루어지는 브랜치. 기능 단위 작업은 dev에 바로 커밋/푸시하고,
  main으로의 반영은 별도로 합의 후 병합한다.

## 7. 구현 완료 API 목록

모든 응답은 `{ "success": bool, "data": ..., "error": { "code", "message" } | null }` 형태로
통일되어 있으며 (`app/schemas/common.py`의 `ApiResponse[T]`), 처리되지 않은 예외까지 포함해
전역 에러 핸들러(`app/core/error_handlers.py`)가 동일한 포맷으로 응답을 감쌉니다.

| 도메인 | 메서드/경로 | 설명 | 인증 |
|---|---|---|---|
| Auth | `POST /api/auth/signup` | 회원가입 | - |
| Auth | `POST /api/auth/login` | 로그인, JWT 발급 | - |
| User | `GET /api/user/me` | 내 정보 조회 | 필요 |
| Submission | `POST /api/submissions` | 내 논문 초안 업로드 | 필요 |
| Paper | `GET /api/papers/{paper_id}` | 기존 논문 상세 조회 | - |
| Review | `GET /api/reviews?paper_id=` | 특정 논문의 리뷰 목록 조회 | - |
| Feedback | `POST /api/feedback/predictions` | 예상 리뷰/수정 제안 생성 (**뼈대만**, 501 반환) | 필요 |

## 8. AI팀 연동 방식

AI팀이 담당하는 분석 로직은 `paper_assistant.analyze()` 형태의 함수(또는 서비스 호출)로
결과를 받아오는 것을 전제로 설계되어 있습니다.

```python
report = paper_assistant.analyze(submission)  # 또는 submission_id
```

- **입력**: 사용자가 올린 `submission` (제목/초록/본문 등)
- **출력**: `Report` — 유사 논문 검색 결과(`similar_paper_matches`)와 예상 리뷰/수정 제안
  (`review_predictions`)을 근거와 함께 담은 결과 객체
- 백엔드 쪽에서는 `app/routers/feedback.py`의 `POST /api/feedback/predictions`가 이 결과를
  받아서 `similar_paper_matches` / `review_predictions` 테이블에 저장하고, 근거(`based_on_matches`)를
  함께 응답으로 내려주는 역할을 맡습니다.
- 정확한 함수 시그니처, `Report` 스키마 필드, AI팀 쪽 코드와의 배포/실행 방식(같은 프로세스 내
  임포트인지, 별도 서비스 호출인지)은 아직 확정되지 않았습니다 (9번 항목 참고).

## 9. 내일 확인사항

**AI팀**
- `analyze()` 함수의 정확한 시그니처 (입력 파라미터, 동기/비동기 여부, 실행 위치)
- `Report` 스키마의 필드 구성 (현재 `review_predictions`/`similar_paper_matches` 테이블
  구조와 맞출 수 있는지)
- DB 공유 방식 — AI팀 코드가 같은 PostgreSQL을 직접 읽고 쓰는지, 아니면 백엔드가 API/함수
  호출로만 결과를 받아 저장하는지

**프론트**
- API 명세 공유 방법 (`/docs`의 Swagger UI 그대로 쓸지, 별도 문서화가 필요한지)
- JWT 사용 방식 (Access Token만 쓸지, Refresh Token 흐름을 프론트에서 어떻게 처리할지,
  토큰 저장 위치)
- CORS 허용 origin 확정 (현재 `app/core/config.py`의 `CORS_ORIGINS` 기본값은
  `http://localhost:3000`, `http://localhost:5173`으로 되어 있음 — 실제 배포 도메인으로 교체 필요)

## 10. 남은 작업

- [ ] Feedback API 실제 구현 (현재는 501을 반환하는 뼈대만 있음)
  - similar_paper_matches 조회/생성 (임베딩 + 벡터 검색)
  - review_predictions 생성 및 저장 (근거 포함)
- [ ] PDF 업로드 (논문 초안을 PDF로 올리는 플로우 — 현재 submission은 텍스트 필드만 지원)
- [ ] DB 세팅 (실제 PostgreSQL 인스턴스 구성, pgvector 확장 설치 여부 결정)
- [ ] 프론트 연동 테스트 (실제 프론트엔드와 CORS/인증/API 응답 포맷 통합 확인)
