#!/usr/bin/env bash
# 노트북에서 DB dump를 복원한다 (data/export/paper_assistant.dump 필요).
# Git Bash / WSL / macOS / Linux 어디서나 동작. PowerShell이면 아래 두 줄을 직접 실행.
#
#   1) docker compose up -d   (컨테이너 먼저 기동)
#   2) bash scripts/restore_db.sh
set -e

DUMP="data/export/paper_assistant.dump"
[ -f "$DUMP" ] || { echo "❌ $DUMP 없음 — 이 컴퓨터에서 옮겨오세요."; exit 1; }

echo "DB 준비 대기..."
until docker exec paper-assistant-db pg_isready -U paper >/dev/null 2>&1; do sleep 1; done

echo "기존 DB 삭제 후 재생성 (init_db.sql이 만든 빈 스키마 제거)..."
docker exec -i paper-assistant-db psql -U paper -d postgres \
  -c "DROP DATABASE IF EXISTS paper_assistant WITH (FORCE);" \
  -c "CREATE DATABASE paper_assistant;"

echo "복원 중 (수 분 소요, HNSW 인덱스 재빌드 포함)..."
docker exec -i paper-assistant-db pg_restore -U paper -d paper_assistant --no-owner < "$DUMP"

echo "검증:"
docker exec paper-assistant-db psql -U paper -d paper_assistant -t \
  -c "SELECT 'papers='||count(*) FROM papers;" \
  -c "SELECT 'review_points='||count(*) FROM review_points;" \
  -c "SELECT 'submission_links='||count(*) FROM submission_links;"
echo "✅ 복원 완료"
