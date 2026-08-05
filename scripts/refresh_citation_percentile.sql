-- papers.citation_percentile 재계산.
--
-- 언제 돌리나: s2_enricher 로 citation_count 를 갱신한 뒤. 한 논문의 인용수가
-- 바뀌면 그 해 전체의 백분위가 흔들리므로 개별 UPDATE 로는 유지할 수 없다.
--
--     docker exec -i paper-assistant-db psql -U paper -d paper_assistant \
--         < scripts/refresh_citation_percentile.sql
--
-- 설계 근거는 docs/랭킹_가중치_설계.md, 최초 생성은 alembic 0010 참고.
-- cume_dist 를 쓰는 이유와 NULL 을 채우지 않는 이유도 그쪽에 적어 두었다.

BEGIN;

-- 인용수가 사라진 논문은 백분위도 지운다 (재보강으로 NULL 이 될 수 있다).
UPDATE papers
SET citation_percentile = NULL
WHERE citation_count IS NULL AND citation_percentile IS NOT NULL;

UPDATE papers p
SET citation_percentile = s.pct
FROM (
    SELECT id,
           cume_dist() OVER (PARTITION BY year ORDER BY citation_count) AS pct
    FROM papers
    WHERE citation_count IS NOT NULL
) s
WHERE p.id = s.id
  AND (p.citation_percentile IS DISTINCT FROM s.pct);

COMMIT;
