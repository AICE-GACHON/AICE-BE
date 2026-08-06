# AICE

ML/AI 논문 리서치 어시스턴트 — 백엔드(FastAPI) + AI 분석 파이프라인이 한 저장소에 있습니다.

사용자가 자신의 논문을 올리면, 비슷한 기존 논문을 찾아 **"그 논문들이 실제로 어떤 지적을
받았는지"** 를 리뷰 원문과 함께 보여줍니다.

**예측이 아니라 열람입니다.** 이 구분은 실측에 근거합니다 — 유사 논문으로 지적 범주를
*예측*하는 것은 '검색 없음' 베이스라인에 졌지만(설계서 §24), 구체적인 지적 *문장*을
근거로 가져오는 것은 잘 됩니다. 그래서 제품이 하는 말은 "당신은 X를 지적받을 것입니다"가
아니라 **"비슷한 논문들은 이런 지적을 받았습니다"** 입니다. 화면 문구도 이 선을 넘지
않아야 합니다.

## 저장소 구성

| 폴더 | 담당 | 역할 |
|---|---|---|
| `app/` | 백엔드 | FastAPI 앱 — 인증, 초안 CRUD, 분석 요청/조회, 코퍼스 조회 API |
| `alembic/` | 백엔드 | 서비스 테이블(users/submissions/온보딩/분석 결과) 마이그레이션 |
| `paper_assistant/` | AI | 검색·분석 파이프라인. 공개 API는 함수 7개뿐 |
| `scripts/` | AI | 코퍼스 스키마(`init_db.sql`)와 운영 배치 (수집·집계·복원) |
| `tests/` | 공통 | `tests/app`(백엔드) + `tests/paper_assistant`(AI) |
| `docs/` | 공통 | 개발 문서 + 설계 근거 + 개편 기록 (아래 "문서" 참고) |

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

342개입니다. 백엔드 테스트는 실제 Postgres를 쓰고 매 테스트를 롤백합니다. DB가 없거나
`alembic upgrade head`를 하지 않았으면 해당 테스트만 자동으로 skip됩니다.

### 8. 프론트엔드 함께 띄우기

백엔드와 나란히 클론해서 각각 띄웁니다.

```bash
git clone https://github.com/AICE-GACHON/AICE-FE.git
cd AICE-FE && npm install && npm run dev
```

프론트는 `.env`의 `VITE_API_BASE_URL`로 백엔드를 찾습니다.

```
VITE_API_BASE_URL=http://localhost:8000
```

⚠️ **이 값이 비어 있으면 화면이 전부 mock 응답으로 돌아갑니다.** 백엔드를 껐는데도
화면이 멀쩡히 동작한다면 십중팔구 이것 때문입니다.

| | 주소 |
|---|---|
| 백엔드 | http://localhost:8000 (Swagger `/docs`) |
| 프론트 | http://localhost:5173 |

백엔드 CORS 허용 목록(`app/core/config.py`의 `CORS_ORIGINS`)에 5173~5175가 이미
들어 있습니다 — 5174/5175는 vite가 포트 충돌 시 자동으로 올라가는 자리입니다.

## 배포 전 점검

배포 설정(Dockerfile 등)은 아직 없지만, **틀리면 공개되는 설정들은 코드가 막습니다.**
`.env`에 `ENVIRONMENT=production`을 넣으면 아래가 전부 기동 시점에 검사되고, 하나라도
어긋나면 서버가 뜨지 않습니다. 경고 로그로 두지 않은 이유는 기동 로그를 아무도 읽지
않기 때문입니다 — 조용히 뜨는 것보다 안 뜨는 편이 낫습니다.

```bash
# .env
ENVIRONMENT=production
JWT_SECRET_KEY=<python -c "import secrets; print(secrets.token_urlsafe(48))">
CORS_ORIGINS=["https://실제-프론트-도메인"]
ALLOWED_HOSTS=["실제-API-도메인"]
```

| 검사 | 통과 못 하면 |
|---|---|
| `JWT_SECRET_KEY`가 `.env.example` 예시 값이거나 32자 미만 | 기동 거부 (환경 무관) |
| `CORS_ORIGINS`에 `*`·`localhost`·`http://`가 남아 있음 | 기동 거부 |
| `ALLOWED_HOSTS`가 `*` | 기동 거부 |
| `ENABLE_DOCS`를 명시하지 않음 | `/docs`·`/openapi.json` 자동으로 닫힘 |

`JWT_SECRET_KEY`를 특히 조심하세요. 이 값이 새면 공격자가 임의 `user_id`로
`access_token`을 만들어 **아무 계정으로나 로그인**할 수 있고, 서버는 그것을 정상
로그인과 구분할 방법이 없습니다. `.env.example`의 문자열은 공개 저장소에 적혀 있으므로
"아직 안 바꿨다"와 "유출됐다"가 같은 뜻입니다.

### 워커를 늘릴 때

rate limit 저장소 기본값은 **프로세스 메모리**입니다. `uvicorn --workers 4`로 띄우면
워커마다 따로 세서 실제 상한이 4배가 됩니다. LLM 예산이 걸린 상한(업로드·분석·`/story`)이
전부 여기 얹혀 있으니, 워커를 늘릴 거면 Redis를 먼저 붙이세요.

```
RATE_LIMIT_STORAGE_URI=redis://호스트:6379
```

### 남아 있는 것

- **프론트가 토큰을 `localStorage`에 둡니다.** XSS가 한 번이라도 성립하면 토큰이 통째로
  털립니다. 현재 프론트에 `dangerouslySetInnerHTML`이나 `eval` 경로는 없지만, 방어가
  "앞으로도 XSS를 안 만든다"에 걸려 있는 상태입니다. httpOnly 쿠키로 옮기려면 백엔드에
  CSRF 방어와 `SameSite` 정책이 함께 와야 해서, 별도 작업으로 남겨 둡니다.
- **`refresh_token`에 재사용 탐지가 없습니다.** 유출되면 만료(14일)까지 유효하고,
  회전 후에도 이전 토큰이 계속 먹힙니다. `token_version`을 매 refresh마다 올리면
  막히지만 그러면 기기 두 대에서 동시에 로그인할 수 없게 되므로, 트레이드오프를
  정한 뒤에 손대야 합니다.
- **HTTPS 종료는 리버스 프록시의 몫입니다.** `ENVIRONMENT=production`이면 HSTS 헤더는
  나가지만, 평문 http로 서비스하면서 켜면 브라우저가 그 도메인을 https로 기억해 버립니다.

## 유사 논문을 어떻게 고르는가 (2단계)

```
PDF 업로드
   ↓ ① 검색   SPECTER2 임베딩 + full-text → RRF → 가중합 재정렬 → 후보 50편
   ↓          (리뷰가 있는 논문 43,034편만. 제목·초록만 본다)
   ↓ ② 판정   입력 PDF **원본** + 후보 50편 → Sonnet 5 → 최대 5편 + 선정 이유
   ↓          (본문·실험·참고문헌까지 읽는다)
   ↓ ③ 조회   그 5편의 리뷰 전문·AC 총평·평점
   ↓ ④ 종합   리뷰 원문을 근거로 한국어 요약 ([E1]/[M1] 인용 검증)
```

**왜 2단계인가.** 검색 상위 50편은 전부 코사인 0.93+ 대역이고 그 안에서 임베딩은
순위를 매기지 못합니다(상위 20편 코사인 폭 0.013). 실측에서 LLM이 고른 5편의 검색
순위가 **15·42·47위**였습니다 — 1위로 올린 검색 15위는 입력 논문과 *동일한* 논문의
ICLR 2022 투고본이었고, 최신성 가중치가 2022년이라는 이유로 내린 것을 되돌렸습니다.
근거와 수치는 [docs/추천_파이프라인_재설계.md](docs/추천_파이프라인_재설계.md) §4.

분석 1회 비용은 **약 $0.30**입니다 (Sonnet 5, 26페이지 PDF 기준. 2026-08-31까지
도입가이고 그 뒤 약 1.5배). PDF는 프롬프트 캐시로 재사용해 종합 호출에서 ~0.1×로
과금됩니다.

## 핵심 흐름

```
POST /api/onboarding                       (선택) 가입 전 익명 온보딩 → onboarding_id
        ↓
POST /api/auth/signup  →  POST /api/auth/login  (JWT)
        ↑ onboarding_id를 실어 보내면 그 답변이 계정에 연결됨
        ↓
POST /api/submissions/pdf                  내 논문 업로드 (PDF 전용, 제목·초록 자동 추출)
        ↓  응답의 page_count가 15를 넘으면 프론트가 "논문 맞나요?" 확인
POST /api/submissions/{id}/analysis        분석 시작 → 202, status=pending
        ↓  (백그라운드에서 paper_assistant.analyze() 실행)
GET  /api/submissions/{id}/analysis        폴링 → status=done 이면 report 포함
        ↓
GET  /api/papers/{paper_id}                선정 논문의 원문·리뷰 전문 (더 깊이 볼 때)
GET  /api/papers/{paper_id}/reviews        그 논문이 받은 리뷰만 (가벼운 조회)
GET  /api/papers/{paper_id}/revisions      그 논문의 저자가 리뷰 후 무엇을 고쳤는지
GET  /api/papers/{paper_id}/story          위 둘을 시간축으로 엮은 "심사 서사"
```

전체 엔드포인트 목록은 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) §7 또는 Swagger에 있습니다.

`/revisions`와 `/story`만 OpenReview API를 실시간으로 조회합니다 — 느리고 실패할 수
있으니 사용자가 명시적으로 눌렀을 때만 호출하세요. **둘 다 IP 기준 30회/시간으로
묶여 있고**, `/story?refresh=true`(캐시 우회)는 로그인이 필요합니다 — LLM을 켜면
캐시에 없는 논문마다 돈이 나가는 유일한 공개 경로이기 때문입니다.

`/story`는 유사 논문을 눌렀을 때 **"이전엔 이랬는데 리뷰를 받고 이렇게 고쳤다"** 를
보여주는 화면용입니다. 재투고 궤적(`journey`) + 리뷰·저자 응답·수정본을 시간순으로
병합한 `timeline` + 요약(`narrative`)이 한 응답에 들어 있어, 상세·리뷰·수정 이력을
따로 부를 필요가 없습니다. 결과는 `paper_stories`에 캐시되므로 두 번째 호출부터
빠릅니다.

## 프론트가 특히 주의할 것

AI 파트가 실측으로 확인한 함정이라 UI에 그대로 반영해야 합니다. 수치와 근거는
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) §6에 있습니다.

- **"이런 지적을 받을 것입니다" 같은 예측형 문구를 쓰면 안 됩니다.** 데이터가 뒷받침하지
  않습니다(§24). 주어는 항상 유사 논문입니다 — "비슷한 논문 N편은 이런 지적을 받았습니다".
- **`selected_papers`가 화면에 보여줄 것이고 `similar_papers`는 후보 풀입니다.**
  `selected_papers`가 비어 있으면 후보로 채우지 말고 "찾지 못했다"고 말해야 합니다 —
  채우는 순간 "비슷한 논문이 받은 리뷰"라는 약속이 거짓이 됩니다.
- **`reviews[].is_unsplit`이 참이면 `weaknesses`에 리뷰 본문 전체가 들어 있습니다.**
  '지적받은 점'이라고 라벨을 붙이면 리뷰 전체가 지적으로 둔갑합니다 — '리뷰 본문'으로
  한 덩어리 표시하세요. 2023년 이전 학회가 전부 여기 해당합니다.
- **"유사도 92%" 같은 UI를 만들면 안 됩니다.** 논문별 유사도 점수는 제공하지 않습니다
  (검색 상위 20개의 코사인 폭이 0.013이라 순위를 정당화할 점수가 안 나옵니다).
  `SelectedPaper.confidence`는 high/medium/low 3단계이고 숫자로 바꾸면 안 됩니다.
- `report.confidence.level`이 `weak`이면 **경고 배너 필수**입니다. 없으면 엉뚱한 주제를
  넣어도 ML 논문을 그럴듯하게 내놓습니다 (이때는 LLM 재정렬을 아예 건너뜁니다).

## 문서

- **[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)** — 폴더 구조, 데이터 모델, API 목록,
  AI 파트 연동 방식, 남은 작업. **처음 읽는다면 여기부터.**
- **[docs/추천_파이프라인_재설계.md](docs/추천_파이프라인_재설계.md)** — 2단계 파이프라인을
  **왜 그렇게 정했는지**. 결정 목록, 후보 수 N의 실측, PDF 토큰 비용 실측, 남은 것.
  구현은 끝났지만 계획서로 남겨 뒀습니다 — 되돌리거나 다시 손볼 때 필요한 것은 결과가
  아니라 근거입니다.
- [docs/AI_파트_설계서.md](docs/AI_파트_설계서.md) — AI 파트가 **왜** 이렇게 됐는지.
  설계 근거, 실측 수치, 실패한 접근, 검색 정확도 평가 결과(§24).
- [docs/랭킹_가중치_설계.md](docs/랭킹_가중치_설계.md) — 검색 랭킹 가중치(유사도·최신성·인용)를
  왜 그 값으로 정했는지. 검토한 3개 안의 비교와 반감기 실측.
- [docs/FRONTEND_심사서사_API.md](docs/FRONTEND_심사서사_API.md) — `/story` 응답을 화면에
  옮기는 방법 (프론트용).

## 알려진 한계

정직하게 적어둡니다. 자세한 근거는 설계서에 §번호로 있습니다.

- **검색이 aspect 예측에서 '검색 없음' 베이스라인에 집니다** (0.66~0.78배, §24).
  다만 구체적인 지적 *문장*을 근거로 가져오는 것은 잘 됩니다 — 강점은 범주 예측이
  아니라 **근거 제시**입니다. → 이 실측이 위 "예측이 아니라 열람" 방향과
  파이프라인 개편의 출발점입니다.
- **⚠️ 후보 50편이 모자랄 수 있습니다.** 실호출 2회에서 LLM이 고른 5편의 검색 순위가
  15·42·47위였습니다 — **47위는 경계에서 3칸**입니다. 표본 1편·2회이고 자기 자신이
  코퍼스에 있는 특수 상황이지만, N=50/75/100 비교(재설계 문서 §8a)를 돌려 확인해야
  합니다. 그때까지는 "정말 비슷한 논문이 후보 밖에 있을 수 있다"가 열린 위험입니다.
- **최신성 가중치는 순위를 크게 흔들지만 유사도는 거의 희생하지 않습니다.** 순수 유사도
  상위 5편 중 top-30에 남는 건 70%뿐이지만, 실제로 보여주는 30편의 평균 코사인은
  **0.004밖에 낮지 않고**(0.9409 → 0.9370) 평균 1.2년 최신입니다. 이 차이는 SPECTER2가
  순위를 못 매기는 폭(상위 20편 내 0.013)보다도 작습니다 — 즉 **밀려난 논문과 들어온 논문을
  우리 임베딩은 구분하지 못합니다**(재설계 문서 §4.3). 그 구간을 판정하는 것이 2단계입니다.
- **"정말 비슷한가"에는 사람 라벨 없는 정답지가 없습니다.** `scripts/eval_retrieval.py`가
  재던 aspect 예측은 목표에서 빠졌고, 그 자리를 채울 자동 평가가 아직 없습니다. 대신
  후보 50편과 선정 결과를 `similar_paper_matches`에 전부 남겨 두었으니, 실사용이 쌓이면
  "LLM이 검색 어디쯤에서 고르는가"를 SQL로 잴 수 있습니다(재설계 문서 §8).
- **제목 추출이 전부 대문자 제목에서 깨집니다.** 드롭캡 복원 정규식이 오작동해
  `L ORA: LOW -RANK ADAPTATION OF LARGE LAN GUAGE MODELS`처럼 나옵니다. 임베딩
  품질에 영향을 주지만 사용자가 업로드 후 확인 화면에서 교정할 수 있습니다.
- **인용은 라벨의 실재성만 검증됩니다** (§23.5). 인용된 원문이 그 문장을 실제로
  뒷받침하는지는 미검증이라, 화면에서 원문을 함께 펼쳐 사용자가 대조하게 해야 합니다.
- **arXiv/S2 보강은 코퍼스 전체를 덮지 못합니다.** 43,515편 중 `arxiv_id` 26,026편
  (59.8%), `s2_paper_id`·`citation_count` 30,238편(69.5%)입니다. 특히 **탈락 논문은
  38.2%**뿐인데, S2가 학회 venue로 채택 논문만 등록해서 제목 기반 보강(by-venue)이
  거의 닿지 않기 때문입니다(채택 논문은 98.1%). 인용 엣지(`citations`)는 아직
  적재하지 않았습니다.
- 위에서 채운 `citation_count`·`final_venue`는 **아직 검색·분석이 읽지 않습니다.**
  저장만 돼 있고 랭킹이나 리포트에 반영되지 않습니다.
- 배포 설정(Dockerfile 등)은 없습니다. 다만 설정 자체의 안전장치는 코드에 있습니다 —
  "배포 전 점검" 참고.
