# AICE-BE

위치 기반 카드 혜택 추천 서비스 백엔드 (FastAPI + PostgreSQL)

## 폴더 구조

```
AICE-BE/
├── app/
│   ├── main.py           # FastAPI 앱 진입점 (여기서 서버 실행)
│   ├── database.py       # DB 연결 설정
│   ├── core/
│   │   └── config.py     # .env 파일에서 환경변수 읽어오는 설정
│   ├── models/            # SQLAlchemy 모델 (테이블 정의) - 도메인별로 분리
│   │   ├── user.py        # users, auth_credentials, user_consents
│   │   ├── card.py         # cards, user_cards
│   │   ├── performance.py  # codef_connections, card_performances
│   │   ├── merchant.py      # merchants, category_mappings
│   │   └── recommendation.py # benefit_clauses, recommendations
│   ├── routers/            # API 엔드포인트 (도메인별 분리, 지금은 임시 뼈대)
│   │   ├── auth.py
│   │   ├── card.py
│   │   ├── performance.py
│   │   ├── merchant.py
│   │   ├── recommendation.py
│   │   └── user.py
│   └── schemas/            # Pydantic 요청/응답 스키마 (앞으로 채워나갈 폴더)
├── alembic/                # DB 마이그레이션 (테이블 생성/변경 이력 관리)
│   └── versions/
│       └── 0001_initial_tables.py   # 최초 테이블 10개 생성 마이그레이션
├── requirements.txt        # 필요한 파이썬 패키지 목록
├── .env.example             # 환경변수 템플릿 (실제 값은 .env에 따로 작성, git에 올리지 않음)
└── .gitignore
```

## 로컬에서 처음 실행하는 법

### 1. 파이썬 가상환경 만들기
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
`.env.example`을 복사해서 `.env` 파일을 만들고, 본인 PostgreSQL 접속 정보로 채우기
```bash
cp .env.example .env
```

### 4. PostgreSQL DB 준비
로컬에 PostgreSQL이 설치돼 있어야 하고, `.env`의 `DATABASE_URL`에 적은 이름으로 빈 데이터베이스를 미리 만들어둬야 합니다.

### 5. 마이그레이션 실행 (테이블 생성)
```bash
alembic upgrade head
```

### 6. 서버 실행
```bash
uvicorn app.main:app --reload
```
브라우저에서 http://localhost:8000 접속하면 `{"service": "AICE-BE", "status": "running"}` 이 보이면 성공.

API 문서는 http://localhost:8000/docs 에서 자동으로 확인 가능 (FastAPI 기본 기능).

## 지금 상태 (2026-07-13 기준)

- [x] 프로젝트 뼈대 (폴더 구조, FastAPI 앱, DB 연결)
- [x] 10개 테이블 SQLAlchemy 모델 작성 완료 (Data 모델링 문서 기준)
- [x] 초기 마이그레이션 파일 작성 완료
- [x] 도메인별 라우터 뼈대 생성 (`/api/{도메인}/ping` 테스트 엔드포인트만 있음)
- [ ] 실제 API 25개 구현 (API 명세서 참고해서 하나씩 채워나가기)

## 다음에 할 일

API 명세서(Notion)에 정리된 순서대로 우선순위 "상"부터 구현:
1. auth 도메인: 회원가입 → 로그인 → 카카오 로그인
2. card 도메인: 카드 카탈로그 조회 → 보유 카드 등록/조회
3. performance 도메인: CODEF 연동 → 수동 실적 입력
4. merchant 도메인: 주변 가맹점 조회
5. recommendation 도메인: 위치 기반 추천 요청

각 라우터 파일(`app/routers/*.py`)의 `ping` 함수를 지우고, 실제 엔드포인트를 하나씩 추가하면 됩니다.
