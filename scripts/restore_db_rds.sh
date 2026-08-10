#!/usr/bin/env bash
# 배포용 덤프를 RDS에 복원한다 (배포_계획.md §9.5-⑤).
# 기존 restore_db.sh는 `docker exec paper-assistant-db`에 의존하므로 RDS에서는 못 쓴다.
# 이 스크립트는 컨테이너 없이 psql/pg_restore 클라이언트로 직접 붙는다.
#
# EC2에서 (Ubuntu 24.04):
#   # ⚠️ Ubuntu 기본 패키지는 16이고, 16의 pg_restore는 17 아카이브를 못 읽는다.
#   #    PGDG 저장소를 붙여야 17이 온다 — 전체 명령은 배포_계획.md §9.5-③.
#   sudo apt install -y postgresql-client-17
#   export PGHOST=paperai-db.c3qou8em6se7.ap-northeast-2.rds.amazonaws.com
#   export PGPASSWORD='...'                   # 비워두면 물어본다
#   bash scripts/restore_db_rds.sh ~/paper_assistant_clean.dump
#
# 환경변수 (전부 기본값 있음, PGHOST만 필수):
#   PGPORT=5432  PGUSER=paper  PGDATABASE=paper_assistant  PGSSLMODE=require
#   JOBS=2             pg_restore 병렬도
#   MAINT_MEM=512MB    복원 role에만 거는 maintenance_work_mem
#   PARALLEL_MAINT=0   복원 role의 max_parallel_maintenance_workers
#
# ⚠️ MAINT_MEM은 JOBS와 곱해진다. 병렬 작업 하나하나가 별도 연결이라 각자 이만큼
#    잡을 수 있다. db.t4g.medium(4 GB)에서 512MB × 2 = 1 GB가 적당하다.
#
# 🔴 PARALLEL_MAINT=0인 이유 — 로컬 리허설에서 실제로 밟았다.
#    pgvector의 **병렬** HNSW 빌드는 maintenance_work_mem만큼을 프로세스 메모리가
#    아니라 **공유 메모리(/dev/shm)**에 잡는다. 모자라면 이렇게 죽는다:
#      ERROR: could not resize shared memory segment ... No space left on device
#    워커를 0으로 두면 그 경로를 아예 타지 않고 private 메모리에서 빌드한다.
#    papers_embedding_hnsw는 169 MB짜리라 직렬로도 몇 분이면 끝난다 — 이 코퍼스에서
#    병렬화로 얻을 게 없다. 4 GB 인스턴스에서 OOM 위험만 늘린다.
set -euo pipefail

DUMP="${1:-data/export/paper_assistant_clean.dump}"

export PGPORT="${PGPORT:-5432}"
export PGUSER="${PGUSER:-paper}"
export PGDATABASE="${PGDATABASE:-paper_assistant}"
export PGSSLMODE="${PGSSLMODE:-require}"
JOBS="${JOBS:-2}"
MAINT_MEM="${MAINT_MEM:-512MB}"
PARALLEL_MAINT="${PARALLEL_MAINT:-0}"

die() { echo "❌ $*" >&2; exit 1; }

# ---------------------------------------------------------------- 사전 점검
command -v pg_restore >/dev/null || die "pg_restore 없음 — sudo apt install -y postgresql-client-17 (§9.5-③)"
command -v psql       >/dev/null || die "psql 없음 — sudo apt install -y postgresql-client-17 (§9.5-③)"

CLIENT_MAJOR="$(pg_restore --version | grep -oE '[0-9]+' | head -1)"
[ "$CLIENT_MAJOR" -ge 17 ] || die "pg_restore가 ${CLIENT_MAJOR}.x다. RDS는 17이라 상위 버전 아카이브를 못 읽는다."

[ -n "${PGHOST:-}" ] || die "PGHOST를 설정하세요 (RDS 엔드포인트)."
[ -f "$DUMP" ] || die "$DUMP 없음 — S3에서 내려받으세요."

# 🔴 §9.6: 전체 덤프에는 users·submissions가 들어 있다. 그대로 복원하면 초대
#    게이트를 통과한 적 없는 개발 계정이 프로덕션에 생긴다.
case "$(basename "$DUMP")" in
  *clean*) ;;
  *) [ "${ALLOW_DIRTY_DUMP:-0}" = "1" ] || die \
"파일명에 'clean'이 없다: $(basename "$DUMP")
   배포용은 data/export/paper_assistant_clean.dump다 (§9.5-⑤).
   개발 계정과 업로드 PDF가 든 전체 덤프일 수 있다.
   그래도 진행하려면 ALLOW_DIRTY_DUMP=1." ;;
esac

if [ -z "${PGPASSWORD:-}" ]; then
  read -rsp "RDS 비밀번호(${PGUSER}@${PGHOST}): " PGPASSWORD; echo
  export PGPASSWORD
fi

q() { psql -qtAX -c "$1"; }

echo "접속 확인: ${PGUSER}@${PGHOST}:${PGPORT}/${PGDATABASE}"
q "SELECT version();" >/dev/null || die "접속 실패 — 보안그룹(paperai-db ← paperai-web)과 sslmode를 확인하세요."

# pgvector가 없으면 복원 도중 타입 오류로 무너진다. 미리 막는다.
[ "$(q "SELECT count(*) FROM pg_extension WHERE extname='vector';")" = "1" ] || die \
"pgvector 확장이 없습니다. 마스터 유저로 먼저:  CREATE EXTENSION vector;  (§9.5-④)"

# 두 번 복원하면 전부 "already exists"로 깨진다. 빈 DB에만 붓는다.
EXISTING="$(q "SELECT count(*) FROM pg_tables WHERE schemaname='public';")"
[ "$EXISTING" = "0" ] || die \
"public 스키마에 이미 테이블이 ${EXISTING}개 있습니다.
   재시도라면 비우고 오세요:  psql -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
   (RDS는 DROP DATABASE 대신 이쪽이 간단합니다)"

# ---------------------------------------------------------------- 복원
# autovacuum 워커가 물려받지 않도록 파라미터 그룹이 아니라 role에 건다 (§9.5-④).
echo "maintenance_work_mem=${MAINT_MEM}, max_parallel_maintenance_workers=${PARALLEL_MAINT} (role ${PGUSER} 한정)"
q "ALTER ROLE \"${PGUSER}\" SET maintenance_work_mem = '${MAINT_MEM}';" >/dev/null
q "ALTER ROLE \"${PGUSER}\" SET max_parallel_maintenance_workers = ${PARALLEL_MAINT};" >/dev/null
# 실패하든 Ctrl-C든 반드시 되돌린다 — 남으면 이 role의 모든 세션이 계속 물고 있다.
reset_role() {
  psql -qtAX -c "ALTER ROLE \"${PGUSER}\" RESET maintenance_work_mem;" >/dev/null 2>&1 || true
  psql -qtAX -c "ALTER ROLE \"${PGUSER}\" RESET max_parallel_maintenance_workers;" >/dev/null 2>&1 || true
}
trap reset_role EXIT

echo "복원 중 (HNSW 인덱스 재빌드 포함 — 로컬 리허설 기준 수 분). jobs=${JOBS}"
# --no-owner/--no-privileges: RDS 마스터는 슈퍼유저가 아니다.
# --no-comments: 아카이브에 COMMENT ON EXTENSION이 vector·pg_trgm 둘 다 들어 있는데,
#   슈퍼유저가 아니면 "must be owner of extension"으로 실패한다.
set +e
pg_restore --no-owner --no-privileges --no-comments --jobs "$JOBS" -d "$PGDATABASE" "$DUMP"
RC=$?
set -e
[ $RC -eq 0 ] || echo "⚠️ pg_restore가 ${RC}로 끝났습니다. 아래 검증 결과로 판단하세요."

reset_role
trap - EXIT
echo "role 설정 원복 완료"

echo "ANALYZE..."
psql -qX -c "ANALYZE;"

# ---------------------------------------------------------------- 검증
echo
echo "== 코퍼스 =="
psql -X -c "SELECT 'papers' t, count(*) FROM papers
  UNION ALL SELECT 'reviews', count(*) FROM reviews
  UNION ALL SELECT 'review_points', count(*) FROM review_points
  UNION ALL SELECT 'submission_links', count(*) FROM submission_links
  UNION ALL SELECT 'paper_stories', count(*) FROM paper_stories;"

echo "== 사용자 데이터 (전부 0이어야 함) =="
DIRTY="$(q "SELECT coalesce(sum(n),0) FROM (
  SELECT count(*) n FROM users
  UNION ALL SELECT count(*) FROM submissions
  UNION ALL SELECT count(*) FROM review_predictions
  UNION ALL SELECT count(*) FROM similar_paper_matches
  UNION ALL SELECT count(*) FROM onboarding_profiles) s;")"
psql -X -c "SELECT 'users' t, count(*) FROM users
  UNION ALL SELECT 'submissions', count(*) FROM submissions
  UNION ALL SELECT 'review_predictions', count(*) FROM review_predictions
  UNION ALL SELECT 'similar_paper_matches', count(*) FROM similar_paper_matches
  UNION ALL SELECT 'onboarding_profiles', count(*) FROM onboarding_profiles;"

PAPERS="$(q "SELECT count(*) FROM papers;")"
ALEMBIC="$(q "SELECT version_num FROM alembic_version;")"
# 덤프는 vector 말고 pg_trgm도 쓴다(검색용 gin_trgm_ops). 둘 다 RDS의 trusted
# extension이라 복원이 알아서 만들지만, 조용히 빠지면 검색만 망가지므로 확인한다.
EXTS="$(q "SELECT string_agg(extname, ' ' ORDER BY extname) FROM pg_extension WHERE extname IN ('vector','pg_trgm');")"
echo "alembic_version = ${ALEMBIC}   (0012가 head — 다음은 alembic upgrade head, §9.5-⑥)"

FAIL=0
[ "$DIRTY" = "0" ] || { echo "🔴 사용자 데이터가 ${DIRTY}행 들어왔습니다. 낡은 덤프입니다 — 지우고 clean 덤프로 다시 하세요."; FAIL=1; }
[ "$PAPERS" -gt 0 ] || { echo "🔴 papers가 비었습니다. 복원이 실패했습니다."; FAIL=1; }
[ "$PAPERS" = "43515" ] || echo "🟡 papers=${PAPERS} (로컬 기준 43515). 덤프가 다르면 정상입니다."
[ -n "$ALEMBIC" ] || { echo "🔴 alembic_version이 비었습니다."; FAIL=1; }
[ "$EXTS" = "pg_trgm vector" ] || { echo "🔴 확장이 모자랍니다 (있는 것: ${EXTS:-없음}). 필요: pg_trgm vector"; FAIL=1; }

[ $FAIL -eq 0 ] && [ $RC -eq 0 ] || exit 1
echo "✅ 복원 완료"
