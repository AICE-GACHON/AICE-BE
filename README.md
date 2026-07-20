# AICE-BE

논문 평가 및 피드백 서비스 백엔드 (FastAPI + PostgreSQL)

사용자가 자신의 논문(초안)을 올리면, 비슷한 기존 논문을 찾아서 그 논문이 받았던 리뷰와
리뷰 이후 어떻게 수정됐는지 보여주고, 이를 바탕으로 예상 리뷰 포인트와 수정 방향을
"근거와 함께" 제시하는 서비스입니다.

## 이 프로젝트가 쓰는 기술, 왜 필요한지

백엔드가 처음이라면 아래 네 가지 역할만 이해하면 됩니다.

| 폴더/파일 | 역할 | 비유 |
|---|---|---|
| `app/models/` | DB 테이블을 파이썬 클래스로 표현 (SQLAlchemy) | 엑셀 시트의 "열 구조"를 코드로 정의한 것 |
| `app/routers/` | 클라이언트(프론트/앱)가 호출하는 API 엔드포인트 | "문 앞 안내데스크" - 요청을 받아서 처리 |
| `app/schemas/` | API 요청/응답으로 주고받는 데이터 형식 (Pydantic) | models가 "DB용 설계도"라면 schemas는 "API 통신용 설계도" |
| `alembic/` | models에서 정의한 테이블을 실제 DB에 만들고, 나중에 구조가 바뀌면 그 변경 이력을 관리 | "DB 구조의 git 같은 것" |

FastAPI가 이 모든 걸 묶어서 `app/main.py`에서 서버로 띄웁니다.

## 폴더 구조

```
AICE-BE/
├── app/
│   ├── main.py           # FastAPI 앱 진입점 (여기서 서버 실행, 라우터 등록)
│   ├── database.py       # DB 연결 설정
│   ├── core/
│   │   └── config.py     # .env 파일에서 환경변수 읽어오는 설정
│   ├── models/            # SQLAlchemy 모델 (테이블 정의) - 도메인별로 분리
│   │   ├── user.py         # users (회원)
│   │   ├── paper.py        # papers (OpenReview에서 수집한 기존 논문 코퍼스)
│   │   ├── review.py       # reviews, revisions (기존 논문이 받은 리뷰 / 수정 이력)
│   │   ├── submission.py   # submissions (내가 올린 논문 초안), similar_paper_matches (유사 논문 검색 결과)
│   │   └── feedback.py     # review_predictions (핵심 산출물: 예상 리뷰/수정 제안)
│   ├── routers/            # API 엔드포인트 (도메인별 분리, 지금은 임시 뼈대)
│   │   ├── auth.py          # 회원가입/로그인
│   │   ├── user.py          # 내 정보
│   │   ├── paper.py         # 기존 논문 코퍼스 조회/검색
│   │   ├── review.py        # 기존 논문의 리뷰/수정이력 조회
│   │   ├── submission.py    # 내 논문 초안 업로드/조회
│   │   └── feedback.py      # 유사 논문 매칭 + 예상 리뷰/수정 제안 (핵심 기능)
│   └── schemas/            # Pydantic 요청/응답 스키마 (앞으로 채워나갈 폴더)
├── alembic/                # DB 마이그레이션 (테이블 생성/변경 이력 관리)
│   └── versions/
│       └── 0001_initial_tables.py   # 최초 테이블 7개 생성 마이그레이션
├── requirements.txt        # 필요한 파이썬 패키지 목록
├── .env.example             # 환경변수 템플릿 (실제 값은 .env에 따로 작성, git에 올리지 않음)
└── .gitignore
```

## 데이터 모델 (테이블) 한눈에 보기

```
users ──< submissions ──< similar_paper_matches >── papers ──< reviews
                │                                       └──< revisions
                └──< review_predictions
```

- **users**: 회원. 이메일/비밀번호로만 로그인 (카카오 로그인 등은 이번 주제에 필요 없어서 뺐습니다).
- **papers**: OpenReview API로 수집한, 이미 심사가 끝난 기존 논문들. 유사 논문 검색의 대상이 되는 "코퍼스"입니다.
- **reviews**: papers가 실제로 받았던 리뷰. 논문 하나에 리뷰가 여러 개 달릴 수 있습니다.
- **revisions**: papers가 리뷰를 받은 뒤 버전별로 어떻게 수정됐는지 기록.
- **submissions**: 사용자가 올린 "내 논문 초안". 아직 리뷰를 받지 않았다는 점이 papers와 다릅니다.
- **similar_paper_matches**: submission과 비슷한 papers를 검색한 결과 (유사도 점수 포함).
- **review_predictions**: 핵심 산출물. "예상 리뷰 포인트 + 수정 제안"을 담되, `based_on_matches`에
  어떤 similar_paper_matches를 근거로 삼았는지 같이 저장합니다. 이건 의도적인 설계입니다 —
  1차 멘토링에서 "RAG가 정답을 바로 주면 안 되고, 판단 근거를 보여줘야 한다"는 피드백을 받아서
  결과에 항상 근거를 붙이도록 만들었습니다.

## 로컬에서 처음 실행하는 법

### 1. 파이썬 가상환경 만들기
가상환경은 "이 프로젝트 전용 파이썬 패키지 창고"입니다. 컴퓨터 전체에 패키지를 깔면
다른 프로젝트와 버전이 충돌할 수 있어서, 프로젝트마다 독립된 공간을 만들어 씁니다.
```bash
python -m venv venv
# Windows
venv\Scripts\activate
# Mac/Linux
source venv/bin/activate
```

### 2. 패키지 설치
```bash
pip install -r requirements.txt
```

### 3. 환경변수 파일 만들기
`.env.example`을 복사해서 `.env` 파일을 만들고, 본인 PostgreSQL 접속 정보로 채우기.
`.env`는 비밀번호 같은 민감한 값이 들어가서 git에는 올리지 않습니다 (`.gitignore`에 이미 등록됨).
```bash
cp .env.example .env
```
`OPENREVIEW_USERNAME`/`OPENREVIEW_PASSWORD`는 https://openreview.net 에서 무료로 계정을
만들면 발급받을 수 있습니다. 논문/리뷰 데이터를 수집하는 스크립트를 만들 때 필요합니다
(지금 당장은 비워둬도 서버 실행에는 문제없습니다).

### 4. PostgreSQL DB 준비
로컬에 PostgreSQL이 설치돼 있어야 하고, `.env`의 `DATABASE_URL`에 적은 이름으로 빈 데이터베이스를 미리 만들어둬야 합니다.

### 5. 마이그레이션 실행 (테이블 생성)
`app/models/`에 파이썬 코드로 적어둔 테이블 설계도를 실제 DB에 반영하는 단계입니다.
```bash
alembic upgrade head
```

### 6. 서버 실행
```bash
uvicorn app.main:app --reload
```
브라우저에서 http://localhost:8000 접속하면 `{"service": "AICE-BE", "status": "running"}` 이 보이면 성공.

API 문서는 http://localhost:8000/docs 에서 자동으로 확인 가능 (FastAPI 기본 기능). 여기서 각
라우터의 `/ping` 엔드포인트를 눌러보면 라우터가 잘 연결됐는지 확인할 수 있습니다.

## 지금 상태 (2026-07-21 기준)

- [x] 프로젝트 뼈대 (폴더 구조, FastAPI 앱, DB 연결)
- [x] 7개 테이블 SQLAlchemy 모델 작성 완료
- [x] 초기 마이그레이션 파일 작성 완료
- [x] 도메인별 라우터 뼈대 생성 (`/api/{도메인}/ping` 테스트 엔드포인트만 있음)
- [ ] OpenReview API로 논문/리뷰/수정이력 수집 스크립트 작성 (특정 학회/연도로 범위 좁혀서 시작)
- [ ] 유사 논문 검색 (임베딩 + 벡터 검색) 구현
- [ ] 서브 RAG(유사 논문 검색 / 리뷰 분석 / 수정이력 분석) + 슈퍼바이저 에이전트 구현
- [ ] 실제 API 구현 (엔드포인트별 요청/응답 스키마 확정 후 하나씩)

## 학술제용 MVP 범위 (중요)

1차 멘토링 피드백을 반영해서, 처음부터 아래 범위로 좁혀서 시작하는 걸 추천합니다.
- **데이터**: 전체 논문이 아니라 특정 학회/연도(예: ICLR 특정 연도)로 한정
- **형식**: 텍스트(논문 초록/리뷰/수정이력) 중심. 그림/이미지 처리, GitHub 코드 연동은
  MVP 이후 확장 아이디어로 남겨둠
- **출력**: 예상 리뷰/수정 제안을 낼 때 항상 "어떤 유사 논문·리뷰를 근거로 했는지" 같이 보여줌
- **투고처 추천(확장 기능)**: "여기 내세요"가 아니라 "비슷한 논문이 어디에 실렸고 어떤 리뷰를
  받았는지" 정보만 제공, 최종 판단은 사용자 몫

## 다음에 할 일

1. `app/schemas/`에 각 도메인별 요청/응답 스키마(Pydantic) 작성
2. auth 도메인부터: 회원가입 → 로그인 (JWT 발급)
3. OpenReview API 연동 스크립트 작성 → papers/reviews/revisions 테이블 채우기
4. submission 도메인: 논문 초안 업로드 → 임베딩 생성
5. feedback 도메인: 유사 논문 검색(similar_paper_matches) → 예상 리뷰/수정 제안(review_predictions) 생성

각 라우터 파일(`app/routers/*.py`)의 `ping` 함수를 지우고, 실제 엔드포인트를 하나씩 추가하면 됩니다.
