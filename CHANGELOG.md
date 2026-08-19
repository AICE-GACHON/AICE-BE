# CHANGELOG

기능 추가/제거를 한 줄씩 기록합니다. 목적은 완전한 릴리스 노트가 아니라 —
**"README/DEVELOPMENT.md 같은 큰 문서를 다시 읽지 않고도 최근에 뭐가 바뀌었는지
아는 것"** 입니다. 이 프로젝트는 별도 버전 태그가 없어 날짜 기준으로 적습니다.

## 기록 규칙

- API 엔드포인트를 추가/제거/변경했으면 한 줄.
- 테이블·컬럼을 추가/제거했으면 한 줄 (alembic 마이그레이션 번호 포함).
- rate limit, 인증 요구사항처럼 클라이언트가 알아야 할 동작이 바뀌었으면 한 줄.
- 오타 수정, 리팩터링, 내부 함수 이름 변경은 적지 않습니다.
- 새로 적은 항목이 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)의 API 표·데이터
  모델과 관련 있으면 그 문서도 같이 고치세요 — 여기 적는 것은 그 문서를 대신하지
  않습니다.

---

## 2026-08-17

- **공유 링크(Share)** 추가: `POST/DELETE /api/submissions/{id}/share`,
  `GET /api/shared/{token}`(비로그인 공개 조회, IP 300회/시간). 새 테이블
  `submission_shares`(`alembic 0018`).
- `/api/papers/{id}/revisions`·`/story`의 rate limit을 **30→100회/시간**으로 상향
  (경로 파라미터가 있는 라우트의 상한이 실제로는 안 걸리던 버그를 고치면서 함께 조정).

## 2026-08-16

- **온보딩 필드 개편** (`alembic 0016`, `0017`): `venue`를 문자열 1개→다중 선택
  리스트(JSONB)로 변경, `similarity_focus`·`recency_bias` 추가(분석 파이프라인에
  실제로 쓰임), 안 쓰이던 `purposes`·`result_order`·`stage` 제거.
- `PATCH /api/user/me/onboarding` 추가 — 마이페이지에서 온보딩 답변 수정(upsert).
- **약관 재동의** 추가: `POST /api/user/me/consent`, `GET /api/legal/{document}`
  (terms/privacy 원문, 비로그인, 60회/분). `users`에 `terms_agreed_at`·
  `terms_version`·`privacy_version` 추가(`alembic 0015`).
- **분석 진행 상황** 추가: `review_predictions.progress`(`alembic 0014`), 폴링
  응답에 단계별 진행 상황이 실림.

## 2026-08-10

- `GET /api/papers/{id}/revisions/body-diff` 추가 — `/revisions`에 본문·그림·표
  단어 단위 diff를 얹은 버전. 캐시 테이블 `paper_body_diffs`(`alembic 0013`).
  IP 30회/시간, `refresh=true`는 로그인 필요.

## 2026-08-08

- 비밀번호 재설정 추가: `POST /api/auth/password/forgot`(5회/분),
  `POST /api/auth/password/reset`(10회/분).

## 2026-08-06

- **파이프라인 2단계 개편**: PDF 전용 입력, 검색 후보 50편 + LLM 재정렬(Sonnet 5,
  최대 5편), 통계 레이어(`review_patterns`/`venue_trends`/`rating_context`) 제거.
  근거는 [docs/추천_파이프라인_재설계.md](docs/추천_파이프라인_재설계.md).

---

*2026-08-19 이전 항목은 이번 문서 감사 때 git/alembic 히스토리로 소급 작성했습니다.
그 이전 히스토리가 필요하면 `git log`를 참고하세요.*
