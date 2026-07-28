# 데모 웹 서버 (임시 프론트)

실제 프론트엔드가 붙기 전까지 **분석 결과를 눈으로 확인하는 화면**이다.
초록을 붙여넣거나 PDF를 올리면 유사 논문 · 리뷰 지적 패턴 · 게재 경향 · 재투고 흐름을 보여준다.

> 이 폴더(`demo/`)는 백엔드(`app/`)와 독립이다. 인증도 DB 쓰기도 없이
> `paper_assistant`의 공개 함수만 호출한다(= 통합 계약 그대로). 프론트가 준비되면
> **이 폴더를 통째로 삭제**하면 된다 — `paper_assistant/`, `app/`에는 영향 없다.

## 실행

```bash
# 1. 의존성 (루트 requirements.txt에 데모용 패키지까지 다 들어 있다)
pip install -r requirements.txt

# 2. DB
docker compose up -d                     # pgvector
#    코퍼스 덤프가 없으면 결과가 비어 있다 — 루트 README "논문 코퍼스 받기" 참고

# 3. 서버
uvicorn demo.server:app --port 8000
#   → http://localhost:8000
```

⚠️ 백엔드 API 서버(`uvicorn app.main:app`)와는 **다른 앱**이다. 같은 포트로 동시에 띄울
수 없으니 하나를 8001로 옮기거나 번갈아 실행할 것. 데모는 로그인이 필요 없고, 백엔드는
JWT 인증이 필요하다.

## 사용

- **제목 + 초록**을 붙여넣거나, **PDF 업로드**(제목/초록 자동 추출)
- 기본은 **무료 모드**($0) — 검색·리뷰 패턴·게재 경향은 실제 데이터, 요약·태깅은 간이 버전
- "AI 요약·태깅 사용" 체크 시 Claude 호출(크레딧 소모) — 유사성 근거 태깅 + Sonnet 종합 요약.
  `.env`에 `ANTHROPIC_API_KEY`가 있어야 동작한다.

## 구조

```
demo/
├── server.py           # FastAPI — /api/analyze가 paper_assistant.analyze() 호출
├── static/index.html   # 단일 페이지 (폼 + 결과 렌더링)
└── README.md
```

기동 시 SPECTER2를 미리 로드한다(수십 초). 그래도 첫 요청이 느리면 CPU 상황 때문이다.
