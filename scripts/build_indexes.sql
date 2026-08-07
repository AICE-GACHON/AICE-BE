-- 벡터 인덱스 생성 — **전체 데이터 적재를 마친 뒤** 실행할 것.
-- 빈 테이블에 미리 만들면 적재 내내 인덱스 갱신 비용이 발생한다.
--
--   docker exec -i paper-assistant-db psql -U paper -d paper_assistant \
--       < scripts/build_indexes.sql
--
-- HNSW 파라미터: m=16, ef_construction=64 (pgvector 기본값).
-- 벡터를 L2 정규화해 저장하므로 코사인 연산자(vector_cosine_ops)를 쓴다.

SET maintenance_work_mem = '1GB';   -- 인덱스 생성 속도에 직접 영향

-- 병렬 빌드는 켜둔 채로 둔다. 예전에는 컨테이너의 /dev/shm(기본 64MB)이 작아
-- "could not resize shared memory segment"로 실패해서 직렬로 빌드했지만,
-- docker-compose.yml에 shm_size: 1gb를 준 뒤로는 필요 없다.

CREATE INDEX IF NOT EXISTS papers_embedding_hnsw
    ON papers USING hnsw (embedding vector_cosine_ops);

-- review_points에는 벡터 인덱스가 없다. 지적항목은 쿼리 시점에 임베딩하므로(§13)
-- 저장할 벡터 자체가 없고, 늘 NULL이던 embedding 컬럼은 alembic 0012에서 지웠다.
-- 사전 임베딩으로 방향을 바꾸면 컬럼과 HNSW를 함께 되살릴 것.

ANALYZE papers;
