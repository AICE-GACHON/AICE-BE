"""분석 실행 서비스 — 백엔드와 AI 파트가 만나는 유일한 지점.

AI 파트의 공개 계약은 함수 두 개뿐입니다.

    from paper_assistant import analyze, get_paper_detail
    report = analyze(title, abstract, pdf_bytes=None, use_llm=None)  # -> Report
    detail = get_paper_detail(paper_id)                              # -> PaperDetail | None

analyze()는 stateless라 DB에 아무것도 쓰지 않습니다. 결과를 사용자·submission에
묶어 저장하는 건 이 모듈의 몫입니다.

왜 백그라운드인가: analyze() 한 번에 SPECTER2 임베딩 로드(첫 호출 수십 초) + 하이브리드
검색 + 집계가 들어갑니다. 요청 안에서 동기로 돌리면 프론트가 그만큼 멈춥니다. 그래서
라우터는 pending 행만 만들고 202로 돌아오고, 실제 실행은 여기서 합니다.
"""
import logging
import uuid
from datetime import datetime, timezone

from app.core.config import settings
from app.database import SessionLocal
from app.models.feedback import ReviewPrediction
from app.models.submission import SimilarPaperMatch, Submission

log = logging.getLogger(__name__)


def _now() -> datetime:
    """DB 컬럼이 timezone 없는 DateTime이라 UTC 기준 naive로 맞춘다."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def run_analysis(prediction_id: uuid.UUID) -> None:
    """백그라운드에서 실행되는 분석 1회분.

    BackgroundTasks가 응답을 보낸 뒤 호출하므로, 요청 세션은 이미 닫혀 있습니다.
    여기서 세션을 새로 열어야 합니다.
    """
    db = SessionLocal()
    try:
        prediction = db.get(ReviewPrediction, prediction_id)
        if prediction is None:
            log.warning("분석 대상 prediction이 없습니다: %s", prediction_id)
            return

        submission = db.get(Submission, prediction.submission_id)
        if submission is None:
            _mark_failed(db, prediction, "분석 대상 논문 초안이 삭제되었습니다.")
            return

        prediction.status = "running"
        db.commit()

        try:
            # 지연 import — torch/SPECTER2를 서버 기동이 아니라 첫 분석 때 로드한다.
            from paper_assistant import analyze

            report = analyze(
                title=submission.title,
                abstract=submission.abstract,
                use_llm=settings.PAPER_ASSISTANT_USE_LLM,
            )
        except Exception as exc:
            # 분석 실패는 서버 장애가 아니라 이 작업의 상태입니다. 사용자가 폴링으로
            # 확인할 수 있도록 failed로 기록하고 예외를 삼킵니다.
            log.exception("analyze() 실패 (prediction_id=%s)", prediction_id)
            _mark_failed(db, prediction, f"{type(exc).__name__}: {exc}")
            return

        prediction.report = report.model_dump(mode="json")
        prediction.confidence_level = report.confidence.level
        prediction.is_reliable = report.confidence.is_reliable
        prediction.explanation_source = "llm" if settings.PAPER_ASSISTANT_USE_LLM else "stub"
        prediction.status = "done"
        prediction.completed_at = _now()

        # 근거 추적: 이 분석이 어떤 논문을 보고 나왔는지 역방향 조회용으로 남긴다.
        for paper in report.similar_papers:
            db.add(
                SimilarPaperMatch(
                    prediction_id=prediction.prediction_id,
                    paper_id=paper.paper_id,
                    rank=paper.rank,
                    match_type=paper.match_type,
                )
            )
        db.commit()
        log.info(
            "분석 완료 prediction_id=%s 유사논문=%d 신뢰도=%s",
            prediction_id, len(report.similar_papers), report.confidence.level,
        )
    except Exception:
        log.exception("분석 작업이 예기치 않게 중단됐습니다 (prediction_id=%s)", prediction_id)
        db.rollback()
    finally:
        db.close()


def _mark_failed(db, prediction: ReviewPrediction, message: str) -> None:
    prediction.status = "failed"
    prediction.error = message
    prediction.completed_at = _now()
    db.commit()
