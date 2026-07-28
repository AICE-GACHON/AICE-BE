# 프로젝트 전체 설명

이 문서 하나로 "지금 무엇이 되고, 무엇이 안 되고, 다음에 무엇을 해야 하는지"를
판단할 수 있도록 정리했다. 수치는 전부 **2026-07-26 기준 실측**이다(추정 아님).

- 설치·실행 → [../README.md](../README.md)
- 폴더/API/데이터 모델 레퍼런스 → [DEVELOPMENT.md](DEVELOPMENT.md)
- 설계 근거와 실패한 접근 → [AI_파트_설계서.md](AI_파트_설계서.md)
- 프론트가 지켜야 할 표시 규칙 → [AI_파트_팀_공유.md](AI_파트_팀_공유.md) §4

---

## 1. 무엇을 하는 서비스인가

사용자가 **아직 투고하지 않은 논문 초안**(제목+초록)을 올리면,

1. 이미 심사가 끝난 ML/AI 논문 43,515편 중 비슷한 것 20편을 찾고,
2. 그 20편이 **실제로 받았던 리뷰**를 분석해서,
3. "이 연구는 어떤 지적을 받을 가능성이 높고, 그 지적을 받은 논문들은 실제로 붙었는가"를
   근거와 함께 보여준다.

핵심 차별점은 예측이 아니라 **근거 추적**이다. 어떤 논문 20편을 봤는지, 그 결과를
믿어도 되는지(`confidence`), LLM이 개입했는지(`used_llm`)를 항상 같이 내려준다.
1차 멘토링에서 지적받은 "RAG를 정답 생성기처럼 쓰는 문제"를 피하기 위한 설계다.

---

## 2. 지금 실제로 되는 것

### 2.1 데이터 자산 (DB 실측)

| 항목 | 수량 | 비고 |
|---|---|---|
| 논문 | **43,515** | 임베딩 43,515 (100%) |
| 리뷰 | **168,217** | rating 보유 168,217 (100%) |
| 리뷰 지적항목 | **1,253,330** | weakness 964,010 / question 289,320 |
| 저자 | 84,270 | |
| 재투고 링크 | 744 | 제목+저자 매칭 기반 |
| venue 통계 | 10 venue | 당락 경계 계산 완료 |
| aspect base rate | 9 aspect | lift 분모 |

수집 범위: **ICLR 2020–2025, NeurIPS 2021–2024** (10 venue, 전부 `done`)

| venue | 논문 | accept율 |
|---|---|---|
| ICLR 2020 | 2,213 | 31.0% |
| ICLR 2021 | 2,594 | 33.1% |
| ICLR 2022 | 2,617 | 41.8% |
| ICLR 2023 | 3,792 | 41.5% |
| ICLR 2024 | 7,404 | 30.5% |
| ICLR 2025 | 11,672 | 31.7% |
| NeurIPS 2021 | 2,768 | **95.1%** |
| NeurIPS 2022 | 2,824 | **94.6%** |
| NeurIPS 2023 | 3,395 | **94.8%** |
| NeurIPS 2024 | 4,236 | **95.3%** |

⚠️ NeurIPS의 95%는 실제 채택률이 아니라 **OpenReview 공개 정책의 산물**이다(실제 ~25%).
그래서 `venue_stats.is_coverage_biased=true`이고 당락 경계(`threshold_50`)를 계산하지 않는다.

### 2.2 분석 1회가 실제로 하는 일

```
초안(제목+초록)
   │
   ├─ SPECTER2 임베딩 (768차원, CPU 약 0.2초 / 첫 로드 수십 초)
   │
   ├─ 하이브리드 검색  ─┬─ pgvector HNSW 코사인 top-50
   │                    └─ Postgres full-text (lexeme OR) top-50
   │                        → RRF(k=60)로 결합 → top-20
   │                        → match_type: both / semantic / lexical
   │
   ├─ 검색 신뢰도 판정   top-5 평균 코사인
   │                     ≥0.93 strong / ≥0.90 moderate / 그 아래 weak
   │
   ├─ [병렬 3분기]
   │   ├─ similarity_tagging  왜 유사한가 (LLM 켤 때만, 아니면 빈 값)
   │   ├─ review_analysis     이웃 20편의 지적을 aspect별 집계
   │   │                      → lift(관측률÷base rate) + 이항검정
   │   │                      → 당락 대조 + Fisher 정확검정
   │   └─ venue_trend         학회별 accept율 + 점수 분포 + 재투고 흐름
   │
   └─ synthesis  → Report (JSON) + 마크다운 요약
```

**소요 시간**: 첫 호출 약 77초(모델 로드 포함), 이후 수 초. 그래서 동기 응답이 아니라
`202 + 폴링` 구조다.

**비용**: 기본 **$0**. `PAPER_ASSISTANT_USE_LLM=1`로 켜면 Haiku(태깅 ~10콜) +
Sonnet(종합 1콜) ≈ 쿼리당 $0.05.

### 2.3 API (전부 동작 확인)

| 메서드/경로 | 인증 | 설명 |
|---|---|---|
| `POST /api/auth/signup` | - | 회원가입 |
| `POST /api/auth/login` | - | JWT 발급 (30분) |
| `GET /api/user/me` | 필요 | 내 정보 |
| `POST /api/submissions` | 필요 | 초안 등록 |
| `GET /api/submissions` | 필요 | 내 초안 목록 (최신순) |
| `GET /api/submissions/{id}` | 필요 | 초안 상세 |
| `DELETE /api/submissions/{id}` | 필요 | 초안 + 분석결과 삭제 |
| `POST /api/submissions/{id}/analysis` | 필요 | 분석 시작 → 202 |
| `GET /api/submissions/{id}/analysis` | 필요 | 상태/결과 폴링 |
| `GET /api/papers/{paper_id}` | - | 코퍼스 논문 상세 |
| `GET /api/papers/{paper_id}/reviews` | - | 그 논문의 리뷰만 |
| `GET /api/papers/{paper_id}/revisions` | - | 저자 수정 이력 (**외부 API**) |

남의 초안 접근은 403이 아니라 **404**다 (존재 여부를 숨긴다).

### 2.4 검증 상태

- 테스트 **165개 통과** — `tests/app` 28개(인증·소유권·분석 상태 전이),
  `tests/paper_assistant` 137개(검색·집계·통계·추출·정규화)
- 백엔드 테스트는 실제 Postgres를 쓰고 매 테스트 롤백. DB 없으면 자동 skip
- **CI 없음** — 로컬에서만 돌린다

---

## 3. 아키텍처에서 알아둘 결정 5개

이유를 모르면 "왜 이렇게 안 했지?" 하고 되돌리기 쉬운 것들이다.

**1. 논문별 유사도 점수를 주지 않는다.**
검색 top-20의 코사인 폭이 0.013뿐이라(0.9378~0.9510) 1위와 20위가 사실상 같은 값이다.
백분위로 변환해도 전부 100에 포화된다. 그래서 `rank` + `match_type`만 준다.
**대신 쿼리 단위 신뢰도는 잘 갈린다** — 도메인 안 0.946~0.966 vs 밖 0.852~0.867로
구간이 겹치지 않아 `confidence`로 쓴다.

**2. 리뷰 지적을 빈도순으로 정렬하지 않는다.**
코퍼스 78.8%가 baselines 지적을 받는다. "20편 중 17편"은 lift 1.08 — 정보량이 0이다.
그래서 base rate 대비 lift + 이항검정으로 재정렬하고, 당락 대조에 Fisher 정확검정을 건다.

**3. 임베딩 클러스터링을 쓰지 않는다.**
SPECTER2는 논문 title+abstract용 모델이라 짧은 리뷰 문장의 유사도가 0.872에 압축된다.
임계값 0.80에서도 한 덩어리로 뭉쳐서, 키워드 aspect 기반 집계로 대체했다.

**4. DB 하나, 소유자 둘.**
서비스 테이블(users/submissions/분석결과)은 alembic이, 코퍼스(papers/reviews/…)는
`scripts/init_db.sql`이 관리한다. `alembic/env.py`의 `CORPUS_TABLES`가 autogenerate에서
코퍼스를 제외하지 않으면 마이그레이션이 코퍼스를 DROP하려 든다.

**5. AI 파트는 별도 서비스가 아니라 같은 프로세스 import.**
공개 계약은 함수 4개(`analyze` / `get_paper_detail` / `get_paper_reviews` /
`get_paper_revisions`)뿐이고, 접점은 `app/services/analysis.py` 한 파일이다.
`analyze()`는 stateless — DB에 아무것도 쓰지 않는다.

---

## 4. 지금 비어 있는 것

### 4.1 프론트 연동 전에 채워야 하는 것

**① PDF 업로드 경로가 백엔드에 없다** ← 가장 큰 구멍
`analyze(pdf_bytes=...)`는 PDF에서 제목/초록을 뽑을 수 있고 `demo/`는 그걸 쓴다.
그런데 `POST /api/submissions`는 JSON만 받는다. 기획서의 주요 입력 방식인데
백엔드 API로는 도달할 수 없다. multipart 경로가 필요하다.

**② `submissions.content`와 `field`가 저장만 되고 쓰이지 않는다**
`run_analysis`는 `analyze(title=..., abstract=...)`만 부른다. 본문 전체(`content`)와
분야(`field`)를 받아 저장하지만 분석에 넘기지 않는다. 쓸 거면 연결하고, 안 쓸 거면
컬럼을 빼는 게 맞다.

**③ 초안 수정(PATCH)이 없다**
올린 뒤 제목/초록을 고칠 수 없다. 지우고 다시 올려야 한다.

**④ 분석 이력 조회가 없다**
`GET /api/submissions/{id}/analysis`는 **가장 최근 1건만** 준다. DB(`review_predictions`)에는
과거 분석이 다 쌓이는데 꺼내볼 API가 없다. "초안을 고쳐서 다시 분석했을 때 뭐가
달라졌나"를 보여주려면 목록 엔드포인트가 필요하다.

**⑤ arXiv / Semantic Scholar 보강이 이 DB에 반영돼 있지 않다**
실측: `arxiv_id` **0건**, `s2_paper_id` **0건**, `citations` **0건**.
설계서 §20에 파이프라인은 만들어져 있지만(`scripts/run_enrichment.py`) 이 DB에는
안 돌았다. 그래서 지금:
- `PaperDetail.arxiv_url`이 **항상 null**
- 인용수(`citation_count`) 없음
- 재투고 흐름이 744건뿐 (제목+저자 매칭만으로 찾은 것)

돌리려면 약 2~3시간(arXiv 하베스트) + 10~20분. `S2_API_KEY`가 필요하다.

### 4.2 운영에 필요한 것

| 항목 | 현재 | 필요한 이유 |
|---|---|---|
| **워커 분리** | `BackgroundTasks`가 API 프로세스 안에서 돎 | 동시 분석이 늘면 API 응답이 같이 느려진다. SPECTER2가 프로세스당 수백 MB |
| **리프레시 토큰** | 없음 (액세스 토큰 30분) | 30분마다 재로그인해야 한다 |
| **페이지네이션** | 없음 | 초안 목록이 전부 한 번에 온다 |
| **rate limiting** | 없음 | 분석은 비싼 연산이다. 무제한 호출 가능 |
| **CI** | 없음 | 테스트가 로컬에서만 돈다 |
| **구조화 로깅/메트릭** | `logging`만 | 분석 실패율·소요시간을 추적할 수 없다 |
| **배포 설정** | 없음 | Dockerfile도, 프로덕션 서버 설정도 없다 |

### 4.3 알고 써야 하는 데이터 한계

**① 지적항목의 65.8%가 `aspect='other'`** (824,663 / 1,253,330)
키워드 사전으로 9개 aspect에 분류하는데, 3분의 2가 어디에도 안 걸린다.
`other`는 패턴 집계에서 제외되므로 그만큼 신호를 버리고 있다.
개선하려면 키워드 사전을 늘리거나 Haiku 분류로 바꿔야 한다.

**② `strength` 지적항목이 하나도 없다**
`ReviewPointDetail.sentiment` 스키마에는 `weakness | strength | question`이 있지만,
추출기는 **weakness와 question만** 만든다. 실측 분포에 `strength`가 0건이다.
강점을 보여주려면 `ReviewDetail.strengths`(원문 텍스트)를 써야 하고, 그것도
분리 포맷(2023년 이후) 리뷰에만 있다.

**③ 리뷰 37%가 강/약점 미분리 포맷** (62,346 / 168,217)
2023년 이전 학회는 리뷰 본문이 한 덩어리다. 머리말(`Cons`, `Weaknesses`)로
약점 섹션을 되살려 **99.9% 복구**했지만(62,300/62,346), 이 항목은
`from_unsplit_review=true`로 표시되며 '지적'이라 단정하면 안 된다.

**④ 코퍼스가 ICLR/NeurIPS뿐**
다른 분야(CV/NLP 전문학회, 비ML 분야)를 넣으면 `confidence=weak`가 나온다.
이건 버그가 아니라 정상 동작이다 — 프론트가 경고를 반드시 띄워야 한다.

**⑤ NeurIPS는 당락 경계를 추정하지 못한다**
표본이 95% accept라 `threshold_50=None`이다. ICLR만 경계값이 있다(5.5~6.5).

---

## 5. 운영 스크립트 — 언제 무엇을 돌리는가

`scripts/`에는 **DB에 쓰는 것만** 남겨뒀다 (설계 단계 탐색용 print 스크립트 10개는 삭제).

| 스크립트 | 언제 | 소요 |
|---|---|---|
| `init_db.sql` | 컨테이너 최초 기동 시 자동 | 즉시 |
| `restore_db.sh` | 새 컴퓨터에서 코퍼스 복원 | 수 분 |
| `build_indexes.sql` | 코퍼스 적재를 **마친 뒤** 1회 | 수 분 (HNSW) |
| `run_ingest.py` (paper_assistant/ingest) | 코퍼스 재수집 | 수 시간 |
| `run_enrichment.py` | arXiv/S2 보강 (**§4.1-⑤, 아직 미실행**) | 2~3시간 |
| `build_base_rates.py` | 수집/재추출 후 lift 분모 재계산 | 수 초 |
| `build_venue_stats.py` | 수집 후 venue 기준선 재계산 | 수 초 |
| `reextract_unsplit.py` | 미분리 리뷰 지적항목 재추출 | 수 분 |

**의존 순서**: 수집 또는 `reextract_unsplit` → `build_base_rates` → (lift가 바뀐다).
`reextract_unsplit`은 이미 적용된 것으로 보인다(미분리 복구율 99.9%).

전부 멱등이라 중간에 끊겨도 다시 실행하면 이어진다.

---

## 6. 결정이 필요한 열린 질문

1. **LLM을 켤 것인가** — 기본 off($0)다. 켜면 유사성 근거 태깅과 Sonnet 종합 요약이
   붙는다(쿼리당 ~$0.05). 시연 품질 vs 예산.
2. **PDF 입력을 지원할 것인가** — 지원하면 §4.1-① multipart 경로가 필요하다.
   안 하면 `pdf/extract.py`와 `analyze(pdf_bytes=)`는 데모 전용으로 남는다.
3. **arXiv/S2 보강을 돌릴 것인가** — arXiv 링크·인용수·정확한 재투고 흐름이 생긴다.
   2~3시간 + S2 API 키.
4. **`content`/`field`를 살릴 것인가 뺄 것인가** (§4.1-②).
5. **배포 환경** — Dockerfile도 프로덕션 설정도 아직 없다. 어디에 올릴지가 정해지면
   워커 분리(§4.2)와 함께 설계해야 한다.
6. **프론트 프레임워크와 담당** — 정해지면 `demo/`를 지우고 CORS origin을 실제
   도메인으로 바꾼다.

---

## 7. 요약: 오늘 기준 완성도

| 영역 | 상태 |
|---|---|
| 논문 수집·임베딩·색인 | ✅ 완료 (43,515편, 100% 임베딩) |
| 검색·분석 파이프라인 | ✅ 완료 + 검증 |
| 통계 보정 (lift, 신뢰도, 표본편향) | ✅ 완료 |
| 백엔드 API (인증/초안/분석/코퍼스) | ✅ 동작 |
| 테스트 | ✅ 165개 (CI는 없음) |
| arXiv/S2 보강 | ⬜ 코드는 있고 **미실행** |
| PDF 업로드 API | ⬜ 없음 (AI 파트는 지원) |
| 초안 수정·분석 이력 API | ⬜ 없음 |
| 프론트 | 🟡 임시 데모만 (`demo/`) |
| 배포 | ⬜ 없음 |

**한 줄로**: AI 파트와 백엔드 API는 실제로 돌아가는 상태이고, 남은 건
**프론트 연동 + 입력 경로(PDF) 보강 + 배포**다.
