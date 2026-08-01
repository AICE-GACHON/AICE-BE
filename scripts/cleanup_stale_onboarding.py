"""회원가입으로 연결되지 않은 채 오래된 익명 온보딩 답변을 정리한다.

POST /api/onboarding는 인증 없이 호출되므로, 온보딩만 하고 가입하지 않은 채
이탈한 행이 user_id=null로 계속 쌓인다. 즉시 삭제할 필요는 없지만(사용자가
나중에 돌아와 회원가입하며 연결할 수도 있음) 무한정 남겨두면 테이블만 커지므로,
일정 기간(기본 30일) 지난 미연결 행만 지운다. cron이나 수동으로 주기적으로 돌리면
된다 (다른 scripts/*.py와 같은 실행 방식).

    $env:PYTHONPATH="."; python scripts/cleanup_stale_onboarding.py
    $env:PYTHONPATH="."; python scripts/cleanup_stale_onboarding.py --days 14
    $env:PYTHONPATH="."; python scripts/cleanup_stale_onboarding.py --dry-run
"""
import argparse
import logging
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models.onboarding import OnboardingProfile

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


def cleanup_stale_onboarding(days: int = 30, dry_run: bool = False) -> int:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    db = SessionLocal()
    try:
        query = db.query(OnboardingProfile).filter(
            OnboardingProfile.user_id.is_(None),
            OnboardingProfile.created_at < cutoff,
        )
        count = query.count()
        if dry_run:
            log.info("삭제 대상 %d건 (dry-run이라 실제로 지우지 않음)", count)
            return count

        query.delete(synchronize_session=False)
        db.commit()
        log.info("미연결 온보딩 답변 %d건 삭제 완료 (%d일 이상 경과)", count, days)
        return count
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="이 일수보다 오래된 미연결 행만 삭제 (기본 30)")
    parser.add_argument("--dry-run", action="store_true", help="삭제하지 않고 대상 건수만 출력")
    args = parser.parse_args()
    cleanup_stale_onboarding(days=args.days, dry_run=args.dry_run)
