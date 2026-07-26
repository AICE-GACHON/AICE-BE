# AICE

ML/AI 논문 리서치 어시스턴트 — 백엔드(FastAPI) + AI 분석 파이프라인이 한 저장소에 있습니다.

사용자가 자신의 논문 초안을 올리면, 비슷한 기존 논문을 찾아 그 논문들이 실제로 받았던
리뷰를 분석해서 **"이 연구가 어떤 지적을 받을지, 어느 학회에서 어떤 평가를 받았는지"** 를
근거와 함께 알려줍니다.

## 저장소 구성

| 폴더 | 담당 | 역할 |
|---|---|---|
| `app/` | 백엔드 | FastAPI 앱 — 인증, 논문 초안 업로드, 분석 요청/조회 API |
| `alembic/` | 백엔드 | 서비스 테이블(users/submissions/분석 결과) 마이그레이션 |
| `paper_assistant/` | AI | 검색·분석 파이프라인. 공개 API는 `analyze()`와 `get_paper_detail()` 둘뿐 |
| `scripts/` | AI | 코퍼스 스키마(`init_db.sql`), 수집·집계 배치, 검증 스크립트 |
| `tests/` | AI | AI 파트 테스트 |
| `demo/` | AI | 통합 계약을 그대로 쓰는 참고용 데모 화면 (독립 실행, 삭제 가능) |

두 파트는 **같은 PostgreSQL 하나**를 씁니다. 논문 코퍼스 테이블(`papers`, `reviews`,
`review_points` …)은 `scripts/init_db.sql`이, 서비스 테이블(`users`, `submissions` …)은
alembic이 관리합니다. 자세한 경계는 [DEVELOPMENT.md](DEVELOPMENT.md) 참고.

## 로컬 실행

### 1. 가상환경 + 패키지

Python 3.13 기준입니다.

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

torch는 CPU 휠로 충분합니다 (GPU 불필요, 용량 절약):

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
```

### 2. 환경변수

```bash
cp .env.example .env
```

`DATABASE_URL`은 기본값(포트 **5433**) 그대로 두세요. 일반 PostgreSQL(5432)을 가리키면
vector 확장도 논문 코퍼스도 없어서 분석이 전부 실패합니다.

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
동기 응답이 아니라 백그라운드 작업 + 폴링 방식입니다 (아래).

## 핵심 흐름

```
POST /api/auth/signup  →  POST /api/auth/login  (JWT)
        ↓
POST /api/submissions                      내 논문 초안 등록
        ↓
POST /api/submissions/{id}/analysis        분석 시작 → 202, status=pending
        ↓  (백그라운드에서 paper_assistant.analyze() 실행)
GET  /api/submissions/{id}/analysis        폴링 → status=done 이면 report 포함
        ↓
GET  /api/papers/{paper_id}                근거로 쓰인 유사 논문 원문·리뷰 전문
GET  /api/papers/{paper_id}/revisions      그 논문의 저자가 리뷰 후 무엇을 고쳤는지
```

마지막 하나만 OpenReview API를 실시간으로 조회합니다 — 느리고 실패할 수 있으니
사용자가 '수정 이력'을 눌렀을 때만 호출하세요.

## 프론트가 특히 주의할 것

AI 파트가 실측으로 확인한 함정이라 UI에 그대로 반영해야 합니다. 근거와 수치는
[AI_파트_팀_공유.md](AI_파트_팀_공유.md) §4에 있습니다.

- **"유사도 92%" 같은 UI를 만들면 안 됩니다.** 논문별 유사도 점수는 제공하지 않습니다
  (검색 상위 20개의 코사인 폭이 0.013이라 순위를 정당화할 점수가 안 나옵니다).
  대신 `rank`와 `match_type`(semantic/lexical/both)을 씁니다.
- `report.confidence.level`이 `weak`이면 **경고 배너 필수**입니다. 없으면 엉뚱한 주제를
  넣어도 ML 논문 20편을 그럴듯하게 내놓습니다.
- 리뷰 지적은 빈도순이 아니라 `is_distinctive`(코퍼스 평균 대비 lift) 기준으로 강조합니다.
- `is_coverage_biased`가 true인 학회는 채택률 절대 수치를 노출하면 안 됩니다
  (NeurIPS는 코퍼스의 95%가 accept로 보이지만 실제 채택률은 ~25%).

## 문서

- [DEVELOPMENT.md](DEVELOPMENT.md) — 폴더 구조, 데이터 모델, API 목록, 파트 간 경계
- [AI_파트_팀_공유.md](AI_파트_팀_공유.md) — AI 파트 요약 (프론트/백엔드용)
- [AI_파트_설계서.md](AI_파트_설계서.md) — 설계 근거, 실험 수치, 실패한 접근
- [ML_AI_논문_RAG_서비스_기획서.md](ML_AI_논문_RAG_서비스_기획서.md) — 서비스 기획
