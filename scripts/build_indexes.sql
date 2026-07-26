-- 벡터 인덱스 생성 — **전체 데이터 적재를 마친 뒤** 실행할 것.
-- 빈 테이블에 미리 만들면 적재 내내 인덱스 갱신 비용이 발생한다.
--
--   docker exec -i paper-assistant-db psql -U paper -d paper_assistant \
--       < scripts/build_indexes.sql
--
-- HNSW 파라미터: m=16, ef_construction=64 (pgvector 기본값).
-- 벡터를 L2 정규화해 저장하므로 코사인 연산자(vector_cosine_ops)를 쓴다.

SET maintenance_work_mem = '1GB';   -- 인덱스 생성 속도에 직접 영향

-- Docker 컨테이너의 /dev/shm(기본 64MB)이 작아 병렬 빌드가
-- "could not resize shared memory segment"로 실패한다. 직렬로 빌드한다.
-- (근본 해결은 docker-compose의 shm_size 확대 — 아래 파일 참고)
SET max_parallel_maintenance_workers = 0;

CREATE INDEX IF NOT EXISTS papers_embedding_hnsw
    ON papers USING hnsw (embedding vector_cosine_ops);

-- review_points.embedding은 현재 전부 NULL(쿼리 시점 임베딩, §13) →
-- HNSW를 만들어도 빈 인덱스라 무의미하므로 생략. 향후 사전 임베딩 시 추가.

ANALYZE papers;
