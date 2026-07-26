"""데모 웹 서버 (팀 시연용, 삭제 가능).

이 폴더(demo/)는 AI 파트와 **완전히 독립**이다. paper_assistant.analyze() 하나만
호출한다 — 즉 백엔드 통합 계약을 그대로 시연한다. 실제 프론트가 준비되면
이 폴더를 통째로 지우면 된다.

실행:
    pip install -r demo/requirements.txt      # AI 파트 requirements도 설치돼 있어야 함
    python -m uvicorn demo.server:app --reload --port 8000
    # 브라우저에서 http://localhost:8000
"""
import logging
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from paper_assistant import analyze, get_paper_detail

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("demo")

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="논문 RAG 데모")


@app.on_event("startup")
def _warmup():
    """SPECTER2/그래프를 미리 로드해 첫 요청 지연을 줄인다."""
    log.info("파이프라인 워밍업 (SPECTER2 로드)...")
    try:
        from paper_assistant.graph.pipeline import build
        build(use_llm=False)
        log.info("워밍업 완료")
    except Exception as e:
        log.warning("워밍업 실패(첫 요청 때 로드됨): %s", e)


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


app.mount("/static", StaticFiles(directory=STATIC), name="static")
