# AICE

ML/AI 논문 리서치 어시스턴트 — 백엔드(FastAPI) + AI 분석 파이프라인이 한 저장소에 있습니다.

사용자가 자신의 논문 초안을 올리면, 비슷한 기존 논문을 찾아 그 논문들이 실제로 받았던
리뷰를 분석해서 **"이 연구가 어떤 지적을 받을지, 어느 학회에서 어떤 평가를 받았는지"** 를
근거와 함께 알려줍니다.

## 저장소 구성

| 폴더 | 담당 | 역할 |
|---|---|---|
| `app/` | 백엔드 | FastAPI 앱 — 인증, 초안 CRUD, 분석 요청/조회, 코퍼스 조회 API |
| `alembic/` | 백엔드 | 서비스 테이블(users/submissions/온보딩/분석 결과) 마이그레이션 |
| `paper_assistant/` | AI | 검색·분석 파이프라인. 공개 API는 함수 6개뿐 |
| `scripts/` | AI | 코퍼스 스키마(`init_db.sql`)와 운영 배치 (수집·집계·복원) |
| `tests/` | 공통 | `tests/app`(백엔드) + `tests/paper_assistant`(AI) |
| `docs/` | 공통 | 개발 문서 2개 (아래 "문서" 참고) |

**화면(프론트엔드)은 이 저장소에 없습니다.** 별도 저장소
[AICE-FE](https://github.com/AICE-GACHON/AICE-FE)(Vite + React)이고, 아래
"8. 프론트엔드 함께 띄우기"대로 나란히 실행합니다.

두 파트는 **같은 PostgreSQL 하나**를 씁니다. 논문 코퍼스 테이블(`papers`, `reviews`,
`review_points` …)은 `scripts/init_db.sql`이, 서비스 테이블(`users`, `submissions` …)은
alembic이 관리합니다. 자세한 경계는 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) 참고.

환경변수는 **`paper_assistant/config.py`가 공유 값(DB·LLM 토글)의 단일 소스**이고,
`app/core/config.py`는 백엔드 전용 값(JWT·CORS·구글 로그인)만 선언합니다.

## 로컬 실행

### 1. 가상환경 + 패키지

Python 3.13+ 기준입니다 (3.14에서도 동작 확인).

```bash
python -m venv venv
venv\Scripts\activate
```

⚠️ **torch를 먼저, CPU 휠로 설치하세요.** 순서를 바꾸면 requirements가 기본 CUDA
빌드(수 GB)를 받아버립니다. GPU는 필요 없습니다.

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements-dev.txt
```

배포 환경이면 테스트 도구가 빠진 `requirements.txt`를 쓰세요.

### 2. 환경변수

```bash
cp .env.example .env
```

`DATABASE_URL`은 기본값(포트 **5433**) 그대로 두세요. 일반 PostgreSQL(5432)을 가리키면
vector 확장도 논문 코퍼스도 없어서 분석이 전부 실패합니다.

`JWT_SECRET_KEY`만 아무 값으로 채우면 서버가 뜹니다. `GOOGLE_CLIENT_ID`는 구글 로그인을
쓸 때만 필요하고, 비어 있으면 `POST /api/auth/google`이 401을 반환합니다.

### 3. DB 띄우기

```bash
docker compose up -d
```

pgvector가 포함된 Postgres 17이 5433 포트로 뜨고, 최초 기동 시 `scripts/init_db.sql`이
자동 실행되어 코퍼스 스키마가 만들어집니다.

### 4. 논문 코퍼스 받기

`docker compose up`은 **빈 스키마만** 만듭니다. 논문 43,515편 / 리뷰 168,217건 /
지적항목 119만 건은 용량 때문에 git에 없고 DB 덤프로 배포합니다.

```bash
bash scripts/restore_db.sh <덤프파일>
```

덤프 위치는 팀 채널을 확인하세요. 코퍼스 없이도 서버는 뜨지만 분석 결과가 비어 있습니다.

### 5. 서비스 테이블 생성

```bash
alembic upgrade head
```

### 6. 서버 실행

```bash
uvicorn app.main:app --reload
```

- 헬스체크: http://localhost:8000
- Swagger: http://localhost:8000/docs

첫 분석 요청에서 SPECTER2 임베딩 모델을 로드하느라 수십 초 걸립니다. 그래서 분석은
동기 응답이 아니라 백그라운드 작업 + 폴링 방식입니다 (아래). 배포 환경에서는
`.env`에 `WARMUP_ON_STARTUP=1`을 주면 이 로드를 기동 시점으로 옮길 수 있습니다.

### 7. 테스트

```bash
pytest
```

198개입니다. 백엔드 테스트는 실제 Postgres를 쓰고 매 테스트를 롤백합니다. DB가 없거나
`alembic upgrade head`를 하지 않았으면 해당 테스트만 자동으로 skip됩니다.

### 8. 프론트엔드 함께 띄우기

백엔드와 나란히 클론해서 각각 띄웁니다.

```bash
git clone https://github.com/AICE-GACHON/AICE-FE.git
cd AICE-FE
npm install
cp .env.example .env      # ← 빠뜨리기 쉽습니다. 아래 경고 참고
npm run dev
```

프론트는 `.env`의 `VITE_API_BASE_URL`로 백엔드를 찾습니다 (`.env`는 gitignore라
클론 직후에는 없습니다 — `.env.example`을 복사해야 합니다).

⚠️ **이 값이 비어 있으면 화면이 에러 없이 조용히 mock 응답으로 동작합니다.**
백엔드를 껐는데도 화면이 멀쩡하거나, 가입·분석은 되는데 DB에 아무것도 안 쌓인다면
십중팔구 `.env`를 안 만든 것입니다.

| | 주소 |
|---|---|
| 백엔드 | http://localhost:8000 (Swagger `/docs`) |
| 프론트 | http://localhost:5173 |

백엔드 CORS 허용 목록(`app/core/config.py`의 `CORS_ORIGINS`)에 5173~5175가 이미
들어 있습니다 — 5174/5175는 vite가 포트 충돌 시 자동으로 올라가는 자리입니다.

## 핵심 흐름

```
POST /api/onboarding                       (선택) 가입 전 익명 온보딩 → onboarding_id
        ↓
POST /api/auth/signup  →  POST /api/auth/login  (JWT)
        ↑ onboarding_id를 실어 보내면 그 답변이 계정에 연결됨
        ↓
POST /api/submissions                      내 논문 초안 등록 (JSON)
POST /api/submissions/pdf                  또는 PDF 업로드 (제목·초록 자동 추출)
        ↓
POST /api/submissions/{id}/analysis        분석 시작 → 202, status=pending
        ↓  (백그라운드에서 paper_assistant.analyze() 실행)
GET  /api/submissions/{id}/analysis        폴링 → status=done 이면 report 포함
        ↓
GET  /api/papers/{paper_id}                근거로 쓰인 유사 논문 원문·리뷰 전문
GET  /api/papers/{paper_id}/reviews        그 논문이 받은 리뷰만 (가벼운 조회)
GET  /api/papers/{paper_id}/revisions      그 논문의 저자가 리뷰 후 무엇을 고쳤는지
```

전체 엔드포인트 목록은 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) §7 또는 Swagger에 있습니다.

`/revisions`만 OpenReview API를 실시간으로 조회합니다 — 느리고 실패할 수 있으니
사용자가 '수정 이력'을 눌렀을 때만 호출하세요.

## 프론트가 특히 주의할 것

AI 파트가 실측으로 확인한 함정이라 UI에 그대로 반영해야 합니다. 수치와 근거는
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) §6에 있습니다.

- **"유사도 92%" 같은 UI를 만들면 안 됩니다.** 논문별 유사도 점수는 제공하지 않습니다
  (검색 상위 20개의 코사인 폭이 0.013이라 순위를 정당화할 점수가 안 나옵니다).
  대신 `rank`와 `match_type`(semantic/lexical/both)을 씁니다.
- `report.confidence.level`이 `weak`이면 **경고 배너 필수**입니다. 없으면 엉뚱한 주제를
  넣어도 ML 논문 20편을 그럴듯하게 내놓습니다.
- 리뷰 지적은 빈도순이 아니라 `is_distinctive`(코퍼스 평균 대비 lift) 기준으로 강조합니다.
- `is_coverage_biased`가 true인 학회는 채택률 절대 수치를 노출하면 안 됩니다
  (NeurIPS는 코퍼스의 95%가 accept로 보이지만 실제 채택률은 ~25%).

## 문서

- **[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)** — 폴더 구조, 데이터 모델, API 목록,
  AI 파트 연동 방식, 남은 작업. **처음 읽는다면 여기부터.**
- [docs/AI_파트_설계서.md](docs/AI_파트_설계서.md) — AI 파트가 **왜** 이렇게 됐는지.
  설계 근거, 실측 수치, 실패한 접근, 검색 정확도 평가 결과(§24).

## 알려진 한계

정직하게 적어둡니다. 자세한 근거는 설계서에 §번호로 있습니다.

- **검색이 aspect 예측에서 '검색 없음' 베이스라인에 집니다** (0.66~0.78배, §24).
  다만 구체적인 지적 *문장*을 근거로 가져오는 것은 잘 됩니다 — 강점은 범주 예측이
  아니라 **근거 제시**입니다.
- **인용은 라벨의 실재성만 검증됩니다** (§23.5). 인용된 원문이 그 문장을 실제로
  뒷받침하는지는 미검증이라, 화면에서 원문을 함께 펼쳐 사용자가 대조하게 해야 합니다.
- arXiv/S2 보강은 코드만 있고 아직 실행하지 않았습니다.
- 배포 설정(Dockerfile 등)은 없습니다.
