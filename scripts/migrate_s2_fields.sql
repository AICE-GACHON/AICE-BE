-- S2/arXiv 보강용 컬럼 추가 마이그레이션 (init_db.sql에도 반영되어 있다).
-- 이미 적재된 DB에 적용:
--   docker compose exec -T db psql -U paper -d paper_assistant < scripts/migrate_s2_fields.sql
-- 멱등 — 여러 번 실행해도 안전하다.

ALTER TABLE papers ADD COLUMN IF NOT EXISTS citation_count INT;

-- by-venue 보강과 인용 엣지 적재에서 s2_paper_id로 역인덱싱한다.
CREATE INDEX IF NOT EXISTS papers_s2 ON papers (s2_paper_id) WHERE s2_paper_id IS NOT NULL;
