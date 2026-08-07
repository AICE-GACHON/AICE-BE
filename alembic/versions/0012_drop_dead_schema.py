"""죽은 컬럼·테이블·인덱스 정리

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-07

스키마 전수 조사에서 **읽는 코드가 하나도 없는** 것들을 걷어낸다. 남겨두면 다음
사람이 "왜 있는지 모르는 컬럼"을 보고 채우려 들거나, 이미 채워진 줄 알고 읽으려
든다 — 실제로 그런 일이 있었다(0008: init_db.sql에만 있고 운영 DB에는 없던
citation_count 때문에 보강이 통째로 실패).

**판정 기준은 하나다 — SELECT 하는 코드가 저장소에 있는가.**

--- 1. 쓴 적도 읽은 적도 없는 것 -------------------------------------------

- `reviews.raw_content`   : init_db.sql이 선언만 하고 upsert_reviews의 INSERT
                            컬럼 목록에 없다. query/timeline.py 주석도 "비어
                            있다"고 기록해 뒀다.
- `review_points.embedding`: 유일한 호출자 run_ingest가 embeddings 인자 없이
                            부르므로 항상 NULL이다. 지적항목 임베딩은 설계상
                            쿼리 시점에 계산한다(설계서 §13).
- `reviews.points_extracted`: load가 true로 세팅만 하고 아무도 WHERE에 쓰지
                            않는다. 재개용 체크포인트로 만들었지만 run_ingest가
                            이 값으로 건너뛰지 않아 기능이 미완성인 채였다.
                            수집 재개는 ingest_status가 담당한다.
- `submissions.content`   : 텍스트 붙여넣기 경로가 사라진 뒤로 항상 NULL인데
                            SubmissionResponse.content로 계속 나가고 있었다.
                            ⚠️ **프론트 응답에서 필드가 사라진다.**

--- 2. 채우기만 하고 아무도 안 읽는 것 --------------------------------------

읽는 쪽이 없으면 채우는 비용(S2 API 호출)만 계속 나간다.

- `citations` 테이블      : s2_enricher가 INSERT하지만 SELECT가 0건.
                            인용 그래프를 쓸 계획이 없음을 확인하고 지운다.
                            (실측상 0행 — --citations를 한 번도 안 돌렸다.)
- `papers.final_venue`    : "최종 게재처"는 실제로 query/journey.py가
                            submission_links를 순회해 계산한다. 같은 개념을 두
                            방식으로 들고 있었고, 컬럼 쪽은 소비자가 없다.
- `authors.s2_author_id`  : 성(姓) 매칭까지 해서 채웠지만 읽는 곳이 없다.

이 둘은 실데이터(final_venue 25,426행 / s2_author_id 57,910행)를 버린다.
되살리려면 s2_enricher를 다시 돌려야 하고 S2 API 호출이 다시 든다 — 그래서
downgrade는 컬럼만 되돌리고 값은 NULL이다.

--- 3. 어떤 쿼리도 타지 않는 인덱스 -----------------------------------------

- `similar_paper_matches_selected` : 0011이 "선정 5편만 꺼내는 조회"를 위해
    부분 인덱스로 만들었지만, 유일한 조회 services/analysis.py의 matches_for()는
    WHERE selected 없이 후보 50편을 전부 가져와 정렬한다. 부분 인덱스는 그
    쿼리에 쓰일 수 없다. 같은 커밋에서 인덱스와 쿼리가 어긋난 경우다.
- `similar_paper_matches_paper`    : paper_id로 거르는 쿼리가 없다.
- `papers_decision`                : WHERE decision 술어가 없다(있는 건
    build_venue_stats의 전체 스캔 집계뿐). 카디널리티도 9라 플래너가 안 쓴다.
- `reviews_pending`                : points_extracted와 함께 사라진다. 부분
    인덱스 조건 안에서 상수인 컬럼을 키로 잡은 인덱스이기도 했다.
- `authors_s2`                     : s2_author_id와 함께 사라진다.

컬럼을 DROP하면 그 컬럼을 참조하는 인덱스는 Postgres가 같이 지운다. 그래서
reviews_pending·authors_s2는 여기서 따로 DROP하지 않는다(downgrade에서는 다시
만들어야 한다).

--- 방어 패턴 --------------------------------------------------------------

코퍼스 테이블(papers/reviews/review_points/authors/citations)은 alembic이 아니라
scripts/init_db.sql이 만든다. 서비스 테이블만 있는 DB(코퍼스 없이 백엔드만 띄운
환경)에 이 마이그레이션을 돌려도 넘어가야 하므로 0008·0010과 같은 to_regclass
가드를 쓴다. submissions·similar_paper_matches는 alembic 소유라 가드가 없다.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- 서비스 테이블 (alembic 소유 — 항상 존재한다) ----------------------
    op.execute("ALTER TABLE submissions DROP COLUMN IF EXISTS content")
    op.execute("DROP INDEX IF EXISTS similar_paper_matches_selected")
    op.execute("DROP INDEX IF EXISTS similar_paper_matches_paper")

    # --- 코퍼스 테이블 (init_db.sql 소유 — 없을 수 있다) -------------------
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.papers') IS NOT NULL THEN
                ALTER TABLE papers DROP COLUMN IF EXISTS final_venue;
                DROP INDEX IF EXISTS papers_decision;
            END IF;

            IF to_regclass('public.reviews') IS NOT NULL THEN
                -- points_extracted를 지우면 reviews_pending도 같이 사라진다.
                ALTER TABLE reviews
                    DROP COLUMN IF EXISTS raw_content,
                    DROP COLUMN IF EXISTS points_extracted;
            END IF;

            IF to_regclass('public.review_points') IS NOT NULL THEN
                ALTER TABLE review_points DROP COLUMN IF EXISTS embedding;
            END IF;

            IF to_regclass('public.authors') IS NOT NULL THEN
                -- s2_author_id를 지우면 authors_s2도 같이 사라진다.
                ALTER TABLE authors DROP COLUMN IF EXISTS s2_author_id;
            END IF;

            DROP TABLE IF EXISTS citations;
        END $$
        """
    )


def downgrade() -> None:
    """구조만 되돌린다 — **값은 돌아오지 않는다.**

    final_venue·s2_author_id에 들어 있던 S2 보강 결과와 submissions.content의
    과거 초안 본문은 upgrade 시점에 사라진다. 필요하면 s2_enricher를 다시 돌릴 것
    (content는 입력 경로 자체가 없어 재생성 불가).
    """
    op.execute("ALTER TABLE submissions ADD COLUMN IF NOT EXISTS content TEXT")
    op.execute(
        "CREATE INDEX IF NOT EXISTS similar_paper_matches_paper "
        "ON similar_paper_matches (paper_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS similar_paper_matches_selected "
        "ON similar_paper_matches (prediction_id, llm_rank) WHERE selected")

    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.papers') IS NOT NULL THEN
                ALTER TABLE papers ADD COLUMN IF NOT EXISTS final_venue TEXT;
                CREATE INDEX IF NOT EXISTS papers_decision ON papers (decision);
            END IF;

            IF to_regclass('public.reviews') IS NOT NULL THEN
                ALTER TABLE reviews
                    ADD COLUMN IF NOT EXISTS raw_content JSONB,
                    ADD COLUMN IF NOT EXISTS points_extracted BOOLEAN
                        NOT NULL DEFAULT false;
                CREATE INDEX IF NOT EXISTS reviews_pending
                    ON reviews (points_extracted) WHERE NOT points_extracted;
            END IF;

            IF to_regclass('public.review_points') IS NOT NULL THEN
                ALTER TABLE review_points
                    ADD COLUMN IF NOT EXISTS embedding vector(768);
            END IF;

            IF to_regclass('public.authors') IS NOT NULL THEN
                ALTER TABLE authors ADD COLUMN IF NOT EXISTS s2_author_id TEXT;
                CREATE INDEX IF NOT EXISTS authors_s2
                    ON authors (s2_author_id) WHERE s2_author_id IS NOT NULL;
            END IF;

            IF to_regclass('public.papers') IS NOT NULL THEN
                CREATE TABLE IF NOT EXISTS citations (
                    citing_paper_id BIGINT NOT NULL
                                    REFERENCES papers(id) ON DELETE CASCADE,
                    cited_paper_id  BIGINT NOT NULL
                                    REFERENCES papers(id) ON DELETE CASCADE,
                    PRIMARY KEY (citing_paper_id, cited_paper_id)
                );
                CREATE INDEX IF NOT EXISTS citations_cited
                    ON citations (cited_paper_id);
            END IF;
        END $$
        """
    )
