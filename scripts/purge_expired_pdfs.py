"""보관 기간이 지난 업로드 PDF 원본을 파기한다 (submissions.pdf_bytes → NULL).

개인정보처리방침(app/legal/privacy.md)이 "업로드 PDF 원본은 분석 완료 후 N일 뒤 파기한다"고
약속하고 있다. 그 약속을 지키는 것이 이 스크립트다 — **이게 안 돌면 방침 문구가
그냥 거짓말이 된다.** cron이나 스케줄러로 하루 한 번 돌리면 된다
(다른 scripts/*.py와 같은 실행 방식).

    $env:PYTHONPATH="."; python scripts/purge_expired_pdfs.py
    $env:PYTHONPATH="."; python scripts/purge_expired_pdfs.py --days 30
    $env:PYTHONPATH="."; python scripts/purge_expired_pdfs.py --dry-run

행을 지우지 않고 **pdf_bytes만 NULL로 비운다.** 제목·초록·분석 결과는 사용자가
계속 보는 자기 기록이라 남겨야 하고, 지워야 하는 것은 원문 자체다. 분석 파이프라인은
pdf_bytes가 없는 초안을 이미 정상 처리한다(paper_assistant/graph/nodes.py) — LLM
재정렬 단계만 건너뛴다. 즉 파기된 초안도 다시 분석할 수 있고, 품질만 낮아진다.

**기준 시각은 업로드가 아니라 "마지막 활동"이다.** 업로드 시각만 보면, 올려두고
석 달 뒤에 처음 분석을 돌리는 사용자의 원문이 그 직전에 사라진다. 그래서
업로드 시각과 마지막 분석 시각 중 **나중 것**을 기준으로 삼는다. 분석을 한 번도
안 돌린 초안은 업로드 시각이 그대로 기준이 된다 (쓰이지도 않은 채 남은 원본이라
오히려 먼저 지워야 한다).

진행 중(pending|running)인 분석이 달린 초안은 건드리지 않는다. 분석은
BackgroundTasks라 요청이 끝난 뒤에 pdf_bytes를 읽으므로, 그 사이에 비우면
실행 중인 분석이 조용히 스텁으로 떨어진다.

⚠️ NULL로 만들어도 디스크가 즉시 줄지는 않는다 — 실제 반환은 autovacuum이 TOAST
테이블을 정리한 뒤다. 급하면 `VACUUM (VERBOSE) submissions;`를 따로 돌릴 것.
"""
import argparse
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models.analysis import IN_PROGRESS, ReviewPrediction
from app.models.submission import Submission

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


def purge_expired_pdfs(days: int = 90, dry_run: bool = False, db=None) -> int:
    """보관 기간이 지난 pdf_bytes를 비운다. 비운 초안 수를 돌려준다.

    db를 넘기면 그 세션을 쓰고, 없으면 직접 연다(운영에서 쓰는 경로). 이 이음매가
    필요한 이유는 테스트다 — 테스트는 바깥 트랜잭션 안에서 돌다가 롤백되므로
    (tests/app/conftest.py db 픽스처), 여기서 SessionLocal()을 새로 열면 다른
    커넥션이 되어 테스트가 만든 데이터가 아예 보이지 않는다. 그러면 "0건 파기"가
    성공처럼 보이고, 파기 로직이 깨져도 테스트가 못 잡는다.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    own_session = db is None
    db = db if db is not None else SessionLocal()
    try:
        # 초안별 마지막 분석 시각. 분석이 없으면 NULL이라, 아래에서 coalesce로
        # 업로드 시각이 그 자리를 대신한다.
        last_analysis = (
            select(func.max(ReviewPrediction.created_at))
            .where(ReviewPrediction.submission_id == Submission.submission_id)
            .correlate(Submission)
            .scalar_subquery()
        )
        # 진행 중인 분석이 하나라도 있으면 제외 (위 docstring 참고).
        running = (
            select(ReviewPrediction.prediction_id)
            .where(ReviewPrediction.submission_id == Submission.submission_id,
                   ReviewPrediction.status.in_(IN_PROGRESS))
            .correlate(Submission)
            .exists()
        )

        expired = (
            Submission.pdf_bytes.isnot(None),
            func.coalesce(last_analysis, Submission.created_at) < cutoff,
            ~running,
        )

        # 비우기 **전에** 센다. 다 비운 뒤에는 세어봐야 0이다.
        count, freed = db.execute(
            select(func.count(), func.coalesce(func.sum(func.length(Submission.pdf_bytes)), 0))
            .where(*expired)
        ).one()

        if dry_run:
            log.info("파기 대상 %d건 / %.1fMB (dry-run이라 실제로 지우지 않음)",
                     count, freed / 1024 / 1024)
            return count

        db.query(Submission).filter(*expired).update(
            {Submission.pdf_bytes: None}, synchronize_session=False)
        db.commit()
        log.info("PDF 원본 %d건 파기 완료 / %.1fMB (마지막 활동 후 %d일 경과)",
                 count, freed / 1024 / 1024, days)
        return count
    finally:
        if own_session:
            db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=90,
        help="마지막 활동(업로드 또는 분석)에서 이 일수가 지난 초안의 PDF를 파기 (기본 90). "
             "app/legal/privacy.md에 적은 보유기간과 같아야 한다")
    parser.add_argument("--dry-run", action="store_true", help="파기하지 않고 대상 건수만 출력")
    args = parser.parse_args()
    purge_expired_pdfs(days=args.days, dry_run=args.dry_run)
