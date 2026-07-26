# 데모 웹 서버 (팀 시연용)

AI 파트가 실제로 동작하는 걸 팀원에게 보여주기 위한 **임시** 화면.
초록을 붙여넣거나 PDF를 올리면 유사 논문 · 리뷰 지적 패턴 · 게재 경향 · 재투고 흐름을 보여준다.

> ⚠️ 이 폴더(`demo/`)는 AI 파트와 **완전히 독립**이다. `paper_assistant.analyze()`
> 함수 하나만 호출한다(= 백엔드 통합 계약 그대로). 실제 프론트가 준비되면
> **이 폴더를 통째로 삭제**하면 된다. `paper_assistant/`에는 영향 없다.

## 실행

```bash
# 1. AI 파트 + DB가 준비돼 있어야 함
pip install -r requirements.txt          # 저장소 루트의 AI 파트 의존성
docker compose up -d                     # pgvector DB
pip install -r demo/requirements.txt     # 데모 전용 (fastapi, uvicorn)

# 2. 서버 실행
python -m uvicorn demo.server:app --port 8000
#   → http://localhost:8000
```

## 사용

- **제목 + 초록**을 붙여넣거나, **PDF 업로드**(제목/초록 자동 추출)
- 기본은 **무료 모드**($0) — 검색·리뷰 패턴·게재 경향은 실제 데이터, 요약·태깅은 간이 버전
- "AI 요약·태깅 사용" 체크 시 Claude 호출(크레딧 소모) — 유사성 근거 태깅 + Sonnet 종합 요약

## 구조

```
demo/
├── server.py           # FastAPI — /api/analyze가 paper_assistant.analyze() 호출
├── static/index.html   # 단일 페이지 (폼 + 결과 렌더링)
├── requirements.txt    # 데모 전용 의존성
└── README.md
```

첫 요청은 SPECTER2 로드로 수십 초 걸린다(서버 시작 시 워밍업하지만 CPU 상황에 따라).
