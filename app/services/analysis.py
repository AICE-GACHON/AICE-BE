"""분석 서비스 — 분석 작업의 생애주기(시작·만료·조회)와 실제 실행.

백엔드와 AI 파트가 만나는 유일한 지점이다. AI 파트의 공개 계약 중 이 모듈이 쓰는
것은 analyze() 하나다.

    from paper_assistant import analyze
    report = analyze(title, abstract)   # -> Report

analyze()는 stateless라 DB에 아무것도 쓰지 않는다. 결과를 사용자·submission에
묶어 저장하는 건 이 모듈의 몫이다.

왜 백그라운드인가: analyze() 한 번에 SPECTER2 임베딩 로드(첫 호출 수십 초) + 하이브리드
검색 + 집계가 들어간다. 요청 안에서 동기로 돌리면 프론트가 그만큼 멈춘다. 그래서
라우터는 pending 행만 만들고 202로 돌아오고, 실제 실행은 여기서 한다.

**작업 등록(BackgroundTasks)은 라우터에 남겨둔다.** 그건 요청-응답 수명주기에
묶인 HTTP 관심사라, 여기로 끌고 오면 서비스가 FastAPI 요청 객체를 알아야 한다.
이 모듈은 "어떤 행을 만들지"까지만 정하고, "언제 실행할지"는 호출자가 정한다.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.analysis import IN_PROGRESS, ReviewPrediction, SimilarPaperMatch
from app.models.onboarding import OnboardingProfile
from app.models.submission import Submission

log = logging.getLogger(__name__)

# 이 시간이 지나도 pending/running이면 죽은 작업으로 본다. BackgroundTasks는
# 프로세스에 묶여 있어서, 서버가 재시작되면 진행 중이던 행이 영원히 running으로
# 남는다 — 정리하지 않으면 그 초안은 두 번 다시 분석할 수 없다.
STALE_AFTER = timedelta(minutes=15)

# 사용자에게 보여줄 실패 문구. 원인 분류는 로그의 몫이다 — 응답에 실리는 문자열은
# 사용자가 할 수 있는 일(다시 시도)만 말한다.
_GENERIC_FAILURE = (
    "분석에 실패했습니다. 잠시 후 다시 시도해 주세요. "
    "계속 실패하면 다른 PDF로 시도해 보세요.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------- 작업 생애주기

def latest_prediction(db: Session, submission_id: uuid.UUID) -> ReviewPrediction | None:
    """가장 최근 분석 1건 (상태 무관). 폴링이 보는 대상이다."""
    return db.scalars(
        select(ReviewPrediction)
        .where(ReviewPrediction.submission_id == submission_id)
        .order_by(ReviewPrediction.created_at.desc())
        .limit(1)
    ).first()


def active_prediction(db: Session, submission_id: uuid.UUID) -> ReviewPrediction | None:
    """진행 중(pending/running)인 분석. 부분 유니크 인덱스가 최대 1건을 보장한다."""
    return db.scalars(
        select(ReviewPrediction)
        .where(ReviewPrediction.submission_id == submission_id,
               ReviewPrediction.status.in_(IN_PROGRESS))
    ).first()


def expire_stale(db: Session, submission_id: uuid.UUID) -> None:
    """오래 매달려 있는 분석을 failed로 내린다 (부분 유니크 인덱스 슬롯도 함께 풀린다)."""
    active = active_prediction(db, submission_id)
    if active is None or active.created_at > _now() - STALE_AFTER:
        return
    active.status = "failed"
    active.error = "분석이 완료되지 않은 채 중단되었습니다 (서버 재시작 등). 다시 시도해 주세요."
    active.completed_at = _now()
    db.commit()


def start_prediction(db: Session,
                     submission_id: uuid.UUID) -> tuple[ReviewPrediction, bool]:
    """분석 행을 확보한다. (prediction, 새로 만들었는가)를 돌려준다.

    두 번째 값이 False면 이미 진행 중인 작업을 그대로 돌려준 것이므로 **호출자는
    백그라운드 작업을 새로 등록하면 안 된다** — 같은 분석이 두 번 돌면 비용도 두
    배고, 두 실행이 같은 행에 결과를 쓴다.

    동시 요청 경합은 DB의 부분 유니크 인덱스가 막고, 그때도 진행 중인 것을 돌려준다.
    """
    expire_stale(db, submission_id)

    existing = active_prediction(db, submission_id)
    if existing is not None:
        return existing, False

    prediction = ReviewPrediction(submission_id=submission_id, status="pending")
    db.add(prediction)
    try:
        db.commit()
    except IntegrityError:
        # 같은 순간에 들어온 다른 요청이 먼저 만들었다 → 그 행을 돌려준다.
        db.rollback()
        existing = active_prediction(db, submission_id)
        if existing is None:
            raise
        return existing, False

    db.refresh(prediction)
    return prediction, True


def matches_for(db: Session, prediction_id: uuid.UUID) -> list[SimilarPaperMatch]:
    """근거로 남긴 유사논문 후보들 (화면에 보일 순서로)."""
    # 선정된 논문이 먼저, 그 안에서 llm_rank 순. 나머지 후보는 검색 순위 순으로
    # 뒤에 붙는다 — 프론트가 앞에서부터 잘라 써도 보여줄 것부터 나오게 한다.
    return list(db.scalars(
        select(SimilarPaperMatch)
        .where(SimilarPaperMatch.prediction_id == prediction_id)
        .order_by(SimilarPaperMatch.selected.desc(),
                  SimilarPaperMatch.llm_rank.nulls_last(),
                  SimilarPaperMatch.rank)
    ).all())


# ---------------------------------------------------------------- 진행 기록

# 진행 이벤트 1건을 progress 배열 **끝에 붙이는** SQL.
#
# ORM으로 읽어서 append하고 다시 넣지 않는 이유가 둘이다:
#   (1) SQLAlchemy는 JSON 값의 제자리 변경을 추적하지 않는다 — prediction.progress
#       .append(...)는 아무 일도 일으키지 않고 조용히 사라진다.
#   (2) 읽고-고쳐-쓰기는 왕복이 두 번이고, 그 사이에 낀 쓰기를 덮어쓴다.
# jsonb의 `||`는 배열끼리 이어 붙이므로 한 번의 UPDATE로 끝난다.
_APPEND_PROGRESS = text("""
    UPDATE review_predictions
    SET progress = progress || CAST(:event AS jsonb)
    WHERE prediction_id = :prediction_id
""")


def _progress_recorder(prediction_id: uuid.UUID):
    """analyze(on_event=...)에 넘길 콜백을 만든다.

    **분석 세션과 다른 세션을 쓴다.** 진행 기록은 분석의 곁가지라, 여기서 난 사고가
    분석 트랜잭션을 오염시키면 안 된다 — 같은 세션을 쓰다가 UPDATE 하나가 실패하면
    그 뒤의 _mark_failed()까지 함께 죽어서, 실패조차 기록하지 못하게 된다.

    ⚠️ 이 콜백은 **분석 스레드에서 동기로** 불린다 (paper_assistant.analyze 주석).
    분석 1회에 10건 남짓이라 왕복 비용은 무시할 만하지만, 여기에 느린 일을 더하면
    그만큼 분석이 늦어진다.
    """
    def record(event) -> None:
        db = SessionLocal()
        try:
            # 배열로 감싸 넘긴다 — jsonb `||`는 객체를 그대로 주면 병합으로 해석할
            # 여지가 있고, 배열끼리면 의미가 하나뿐이다.
            db.execute(_APPEND_PROGRESS,
                       {"event": json.dumps([event.model_dump(mode="json")]),
                        "prediction_id": prediction_id})
            db.commit()
        finally:
            db.close()

    return record


# ------------------------------------------------------------------ 실제 실행


def _preferences_for(db: Session, user_id: uuid.UUID):
    """이 사용자의 온보딩 답변 → analyze()에 넘길 검색 선호.

    온보딩 행이 없을 수 있다. 초대 코드로 바로 가입했거나, 온보딩을 건너뛰었거나,
    익명으로 만든 onboarding_id가 회원가입 요청에 실려 오지 않아 user_id가 끝내
    채워지지 않은 경우다(app/models/onboarding.py). **없으면 기본값이고, 그게 곧
    "균형있게"다** — 프론트 기본값과 같은 뜻이라 별도 처리가 필요 없다.

    ⚠️ **DB 값을 그대로 신뢰하지 않는다.** POST /api/onboarding은 인증이 없고
    컬럼은 자유 문자열 String(50)이라 무엇이든 들어올 수 있다. 화이트리스트 검사는
    SearchPreferences가 한다 — 여기서는 읽어서 넘기기만 한다.

    조회 실패로 분석을 죽이지 않는다. 선호는 분석의 곁가지고, 못 읽으면 기본값으로
    도는 것이 정답이다 — 이걸 못 읽어서 분석 전체가 실패하면 앞뒤가 바뀐 것이다.
    """
    from paper_assistant.schemas import SearchPreferences

    try:
        profile = db.scalars(
            select(OnboardingProfile)
            .where(OnboardingProfile.user_id == user_id)
        ).first()
    except Exception:
        log.warning("온보딩 선호 조회 실패 (user_id=%s) — 기본값으로 분석합니다",
                    user_id, exc_info=True)
        return SearchPreferences()

    if profile is None:
        return SearchPreferences()
    return SearchPreferences(similarity_focus=profile.similarity_focus,
                             recency_bias=profile.recency_bias)


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

        # 온보딩 선호는 **분석을 시작하는 이 시점의 값**으로 고정된다. 사용자가
        # 분석이 도는 동안 마이페이지에서 답을 바꿔도 이번 결과는 흔들리지 않고,
        # 무엇을 적용했는지는 report.preferences에 남는다.
        preferences = _preferences_for(db, submission.user_id)

        try:
            # 지연 import — torch/SPECTER2를 서버 기동이 아니라 첫 분석 때 로드한다.
            # use_llm은 넘기지 않는다: 설정은 paper_assistant.config 하나가 소유하고,
            # 실제로 LLM을 썼는지는 아래에서 report.used_llm으로 확인한다.
            from paper_assistant import analyze

            # PDF 원본을 함께 넘긴다. 1단계 검색은 제목·초록만 쓰지만 2단계 LLM
            # 재정렬이 본문·참고문헌을 봐야 하기 때문이다. 개편 이전에 만들어진
            # 초안은 pdf_bytes가 None이라 제목·초록만으로 돌아간다.
            #
            # on_event로 단계마다 progress 컬럼이 자라고, 폴링이 그것을 그대로
            # 실어 나른다. 여기서 예외가 나도 분석은 계속된다(analyze가 삼킨다).
            report = analyze(title=submission.title, abstract=submission.abstract,
                             pdf_bytes=submission.pdf_bytes,
                             on_event=_progress_recorder(prediction_id),
                             preferences=preferences)
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
