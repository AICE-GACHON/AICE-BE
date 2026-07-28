"""데모 웹 서버 (팀 시연용 / 임시 프론트).

실제 프론트엔드가 붙기 전까지 **분석 결과를 눈으로 확인하는 유일한 화면**이다.
이 폴더(demo/)는 백엔드(app/)와 독립이다 — 인증도 DB 쓰기도 없이
paper_assistant의 공개 함수만 호출한다. 즉 통합 계약을 그대로 시연한다.

프론트가 준비되면 이 폴더는 지워도 된다 (`paper_assistant/`, `app/`에 영향 없음).

실행 (루트의 requirements.txt만 설치돼 있으면 된다):
    uvicorn demo.server:app --reload --port 8000
    # 브라우저에서 http://localhost:8000

⚠️ 백엔드 API 서버(`uvicorn app.main:app`)와는 **다른 앱**이다. 같은 포트로 동시에
띄울 수 없으니 하나를 8001로 옮기거나 번갈아 실행할 것.
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from paper_assistant import analyze, get_paper_detail, get_paper_revisions

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("demo")

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """SPECTER2/그래프를 미리 로드해 첫 요청 지연을 줄인다.

    warmup()은 analyze()가 쓰는 것과 **같은 캐시**를 채운다. 예전에는 build()를
    직접 불러서, 여기서 만든 모델이 버려지고 첫 분석이 SPECTER2를 한 번 더
    로드했다 (워밍업이 오히려 두 배로 느렸다).
    """
    log.info("파이프라인 워밍업 (SPECTER2 로드)...")
    try:
        from paper_assistant.graph.pipeline import warmup

        warmup(use_llm=False)
        log.info("워밍업 완료")
    except Exception as e:
        log.warning("워밍업 실패(첫 요청 때 로드됨): %s", e)
    yield


app = FastAPI(title="논문 RAG 데모", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.post("/api/analyze")
async def api_analyze(
    title: str = Form(""),
    abstract: str = Form(""),
    use_llm: str = Form("0"),
    pdf: UploadFile | None = File(None),
):
    pdf_bytes = await pdf.read() if pdf is not None else None
    if not (title or abstract or pdf_bytes):
        return JSONResponse(
            {"error": "제목/초록을 입력하거나 PDF를 업로드하세요."}, status_code=400)
    try:
        report = analyze(
            title=title, abstract=abstract, pdf_bytes=pdf_bytes,
            use_llm=(use_llm == "1"),
        )
    except Exception as e:
        log.exception("analyze 실패")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return JSONResponse(report.model_dump())


@app.get("/api/paper/{paper_id}")
def api_paper(paper_id: int):
    """유사 논문 상세 (초록·메타리뷰·리뷰 전문). 목록에서 펼칠 때 lazy load."""
    try:
        detail = get_paper_detail(paper_id)
    except Exception as e:
        log.exception("get_paper_detail 실패")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    if detail is None:
        return JSONResponse({"error": "논문을 찾을 수 없습니다."}, status_code=404)
    return JSONResponse(detail.model_dump())


@app.get("/api/paper/{paper_id}/revisions")
def api_paper_revisions(paper_id: int):
    """수정 이력 (저자가 리뷰 받고 뭘 고쳤는지). OpenReview를 실시간 조회하므로
    상세보다 느리다 — '수정 이력 보기'를 눌렀을 때만 호출된다."""
    try:
        revs = get_paper_revisions(paper_id)
    except Exception as e:
        log.exception("get_paper_revisions 실패")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    if revs is None:
        return JSONResponse({"error": "논문을 찾을 수 없습니다."}, status_code=404)
    return JSONResponse(revs.model_dump())


app.mount("/static", StaticFiles(directory=STATIC), name="static")
