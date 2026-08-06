"""분석 실행 서비스 — 백엔드와 AI 파트가 만나는 유일한 지점.

AI 파트의 공개 계약은 함수 네 개뿐이고, 그중 이 모듈이 쓰는 건 analyze() 하나다.

    from paper_assistant import analyze
    report = analyze(title, abstract)   # -> Report

analyze()는 stateless라 DB에 아무것도 쓰지 않는다. 결과를 사용자·submission에
묶어 저장하는 건 이 모듈의 몫이다.

왜 백그라운드인가: analyze() 한 번에 SPECTER2 임베딩 로드(첫 호출 수십 초) + 하이브리드
검색 + 집계가 들어간다. 요청 안에서 동기로 돌리면 프론트가 그만큼 멈춘다. 그래서
라우터는 pending 행만 만들고 202로 돌아오고, 실제 실행은 여기서 한다.
"""
import logging
import uuid
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.analysis import ReviewPrediction, SimilarPaperMatch
from app.models.submission import Submission

log = logging.getLogger(__name__)

# 사용자에게 보여줄 실패 문구. 원인 분류는 로그의 몫이다 — 응답에 실리는 문자열은
# 사용자가 할 수 있는 일(다시 시도)만 말한다.
_GENERIC_FAILURE = (
    "분석에 실패했습니다. 잠시 후 다시 시도해 주세요. "
    "계속 실패하면 다른 PDF로 시도해 보세요.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def run_analysis(prediction_id: uuid.UUID) -> None:
    """백그라운드에서 실행되는 분석 1회분.

    BackgroundTasks가 응답을 보낸 뒤 호출하므로, 요청 세션은 이미 닫혀 있다.
    여기서 세션을 새로 열어야 한다.
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
            # use_llm은 넘기지 않는다: 설정은 paper_assistant.config 하나가 소유하고,
            # 실제로 LLM을 썼는지는 아래에서 report.used_llm으로 확인한다.
            from paper_assistant import analyze

            # PDF 원본을 함께 넘긴다. 1단계 검색은 제목·초록만 쓰지만 2단계 LLM
            # 재정렬이 본문·참고문헌을 봐야 하기 때문이다. 개편 이전에 만들어진
            # 초안은 pdf_bytes가 None이라 제목·초록만으로 돌아간다.
            report = analyze(title=submission.title, abstract=submission.abstract,
                             pdf_bytes=submission.pdf_bytes)
        except Exception:
            # 분석 실패는 서버 장애가 아니라 이 작업의 상태다. 사용자가 폴링으로
            # 확인할 수 있도록 failed로 기록하고 예외를 삼킨다.
            #
            # ⚠️ **예외 문자열을 그대로 저장하지 않는다.** 이 값은 GET
            # /api/submissions/{id}/analysis의 error 필드로 사용자에게 그대로
            # 나간다. psycopg의 OperationalError에는 DB 호스트·포트·사용자명이,
            # anthropic 예외에는 요청 URL과 응답 본문이, 파일 관련 예외에는 서버
            # 경로가 들어 있다 — 전부 공격자가 다음 수를 정하는 데 쓰는 정보다.
            # 진짜 원인은 로그(스택트레이스 포함)에만 남긴다.
            log.exception("analyze() 실패 (prediction_id=%s)", prediction_id)
            _mark_failed(db, prediction, _GENERIC_FAILURE)
            return

        prediction.report = report.model_dump(mode="json")
        prediction.confidence_level = report.confidence.level
        prediction.is_reliable = report.confidence.is_reliable
        # 설정값이 아니라 실행 결과를 적는다 — "켠 줄 알았는데 스텁이 나온" 경우를
        # 구분할 수 있어야 근거 추적이 의미가 있다.
        prediction.explanation_source = "llm" if report.used_llm else "stub"
        prediction.status = "done"
        prediction.completed_at = _now()

        # 근거 추적: 검색 후보 전부와, 그중 LLM이 고른 것을 남긴다.
        #
        # 후보까지 남기는 이유는 **"검색이 뽑은 것"과 "LLM이 고른 것"의 관계가 이
        # 파이프라인의 품질 지표**이기 때문이다. 실호출에서 LLM이 검색 42·47위를
        # 고른 사례가 나왔고(재설계 문서 §4.4.1), 그런 일이 계속 생기면 후보 수를
        # 늘려야 한다는 신호다. 유사도에는 사람 라벨 없는 정답지가 없으므로 실사용
        # 분석에서 모이는 이 신호가 현실적으로 유일한 대규모 평가 데이터다.
        chosen = {p.paper_id: p for p in report.selected_papers}
        for paper in report.similar_papers:
            pick = chosen.get(paper.paper_id)
            db.add(SimilarPaperMatch(
                prediction_id=prediction.prediction_id,
                paper_id=paper.paper_id,
                rank=paper.rank,                       # 검색 순위
                match_type=paper.match_type,
                selected=pick is not None,
                llm_rank=pick.rank if pick else None,  # 화면 순서
                selection_reason=pick.reason if pick else None,
            ))
        db.commit()
        log.info("분석 완료 prediction_id=%s 유사논문=%d 신뢰도=%s",
                 prediction_id, len(report.similar_papers), report.confidence.level)
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
