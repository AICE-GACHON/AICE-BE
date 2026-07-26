"""코퍼스 전체 aspect base rate를 계산해 `aspect_base_rates`에 적재한다.

리뷰 패턴의 lift 계산에 쓰인다 (설계서 §18). 수집이 끝난 뒤 1회 실행하고,
이후 데이터가 크게 늘면 다시 돌리면 된다. 96만 건 집계라 수 초 걸린다.

    $env:PYTHONPATH="."; python scripts/build_base_rates.py
"""
import logging

from paper_assistant.db.connection import cursor

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

DDL = """
CREATE TABLE IF NOT EXISTS aspect_base_rates (
    aspect       TEXT NOT NULL,
    sentiment    TEXT NOT NULL,
    paper_count  BIGINT NOT NULL,
    total_papers BIGINT NOT NULL,
    base_rate    REAL   NOT NULL,
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (aspect, sentiment)
);
"""

# 분모는 "**모든 리뷰가 파싱된** 논문 수"다. 리뷰 일부만 파싱된 논문을 넣으면
# 안 된다 — 강/약점 미분리 리뷰 중 약점 섹션을 못 살린 것은 지적을 볼 수 없어
# 거짓 음성이 되고, base rate가 계통적으로 과소평가된다. 실측: 부분 복구 논문을
# 포함하면 미분리 논문의 지적률이 분리 포맷 대비 0.55~0.76배로 내려앉지만,
# 완전 파싱 논문만 비교하면 0.79~1.04배로 정상이다.
MEASURABLE = """
CREATE TEMP TABLE measurable_papers AS
SELECT r.paper_id
FROM reviews r
LEFT JOIN (
    SELECT DISTINCT review_id FROM review_points WHERE sentiment = %(sentiment)s
) x ON x.review_id = r.id
GROUP BY r.paper_id
HAVING bool_and(NOT r.needs_llm_split OR x.review_id IS NOT NULL)
"""
MEASURABLE_INDEX = "CREATE INDEX ON measurable_papers (paper_id)"

COMPUTE = """
INSERT INTO aspect_base_rates (aspect, sentiment, paper_count, total_papers, base_rate)
SELECT rp.aspect,
       %(sentiment)s,
       count(DISTINCT rp.paper_id),
       t.total,
       count(DISTINCT rp.paper_id)::real / NULLIF(t.total, 0)
FROM review_points rp
JOIN measurable_papers m ON m.paper_id = rp.paper_id
CROSS JOIN (SELECT count(*) AS total FROM measurable_papers) t
WHERE rp.sentiment = %(sentiment)s
GROUP BY rp.aspect, t.total
ON CONFLICT (aspect, sentiment) DO UPDATE
SET paper_count  = EXCLUDED.paper_count,
    total_papers = EXCLUDED.total_papers,
    base_rate    = EXCLUDED.base_rate,
    computed_at  = now();
"""


def main(sentiment: str = "weakness") -> None:
    with cursor() as cur:
        cur.execute(DDL)
        cur.execute(MEASURABLE, {"sentiment": sentiment})
        cur.execute(MEASURABLE_INDEX)
        cur.execute(COMPUTE, {"sentiment": sentiment})
        cur.execute(
            "SELECT aspect, paper_count, total_papers, base_rate "
            "FROM aspect_base_rates WHERE sentiment = %s ORDER BY base_rate DESC",
            (sentiment,))
        rows = cur.fetchall()

    print(f"\n{'aspect':24} {'논문수':>8} {'분모':>8} {'base rate':>10}")
    print("-" * 54)
    for aspect, cnt, total, rate in rows:
        print(f"{aspect:24} {cnt:8,} {total:8,} {rate*100:9.1f}%")
    print(f"\n✅ {len(rows)}개 aspect 적재 완료 (sentiment={sentiment})")


if __name__ == "__main__":
    main()
