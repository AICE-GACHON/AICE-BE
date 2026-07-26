"""venue별 rating 기준선과 당락 경계를 계산해 `venue_stats`에 적재한다.

원점수(rating)를 그대로 보여주면 오해를 부른다 (설계서 §19):
  - ICLR 2020은 1~8 척도, 나머지는 1~10 척도다.
  - 같은 6.0이라도 ICLR 2025에선 통과권(66%)이지만 NeurIPS 2024 코퍼스에선 평범하다.
따라서 venue별 기준선을 미리 계산해두고, 쿼리 시점에는 '기준선 대비'로만 말한다.

**표본 편향 경고**: NeurIPS는 코퍼스의 95%가 accept다 — OpenReview가 채택 논문
위주로만 공개하기 때문이고, 실제 채택률(~25%)이 아니다. 이런 venue는
`is_coverage_biased`를 세워 절대 accept율·당락 경계를 신뢰하지 않게 한다.

    $env:PYTHONPATH="."; python scripts/build_venue_stats.py
"""
import logging

from paper_assistant.db.connection import cursor

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# reject 표본이 이보다 적으면 당락 경계를 추정하지 않는다 (NeurIPS는 4.7%).
MIN_REJECT_SHARE = 0.15
# 경계 추정 시 버킷당 최소 표본. 꼬리의 우연한 역전을 막는다.
MIN_BUCKET_N = 30

DDL = """
CREATE TABLE IF NOT EXISTS venue_stats (
    venue               TEXT PRIMARY KEY,
    papers              BIGINT NOT NULL,
    accept_count        BIGINT NOT NULL,
    reject_count        BIGINT NOT NULL,
    accept_rate         REAL   NOT NULL,
    rating_mean         REAL,
    rating_sd           REAL,
    accept_rating_mean  REAL,
    reject_rating_mean  REAL,
    scale_max           REAL,
    threshold_50        REAL,      -- 통과율 50%를 넘기는 최저 평균 rating
    is_coverage_biased  BOOLEAN NOT NULL DEFAULT false,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# 논문 단위 평균 rating. 리뷰 수가 논문마다 달라 리뷰 단위로 평균내면
# 리뷰가 많은 논문에 가중치가 쏠린다.
BASE = """
CREATE TEMP VIEW paper_rating AS
SELECT p.id, p.venue, p.decision,
       (p.decision LIKE 'accept%') AS accepted,
       avg(r.rating) AS avg_rating
FROM papers p JOIN reviews r ON r.paper_id = p.id
WHERE r.rating IS NOT NULL
GROUP BY p.id, p.venue, p.decision;
"""

AGG = """
SELECT venue,
       count(*),
       count(*) FILTER (WHERE accepted),
       count(*) FILTER (WHERE decision = 'reject'),
       avg(avg_rating), stddev(avg_rating),
       avg(avg_rating) FILTER (WHERE accepted),
       avg(avg_rating) FILTER (WHERE decision = 'reject'),
       max(avg_rating)
FROM paper_rating GROUP BY venue ORDER BY venue;
"""

# 0.5 단위 버킷별 통과율 — 당락 경계 추정용
BUCKETS = """
SELECT round(avg_rating * 2) / 2 AS bucket,
       count(*) AS n,
       count(*) FILTER (WHERE accepted)::real / count(*) AS accept_rate
FROM paper_rating
WHERE venue = %s AND (accepted OR decision = 'reject')
GROUP BY 1 HAVING count(*) >= %s ORDER BY 1;
"""

UPSERT = """
INSERT INTO venue_stats (venue, papers, accept_count, reject_count, accept_rate,
                         rating_mean, rating_sd, accept_rating_mean,
                         reject_rating_mean, scale_max, threshold_50,
                         is_coverage_biased)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (venue) DO UPDATE SET
    papers = EXCLUDED.papers, accept_count = EXCLUDED.accept_count,
    reject_count = EXCLUDED.reject_count, accept_rate = EXCLUDED.accept_rate,
    rating_mean = EXCLUDED.rating_mean, rating_sd = EXCLUDED.rating_sd,
    accept_rating_mean = EXCLUDED.accept_rating_mean,
    reject_rating_mean = EXCLUDED.reject_rating_mean,
    scale_max = EXCLUDED.scale_max, threshold_50 = EXCLUDED.threshold_50,
    is_coverage_biased = EXCLUDED.is_coverage_biased, computed_at = now();
"""


def _threshold(cur, venue: str) -> float | None:
    """통과율이 50%를 처음 넘는 평균 rating. 단조롭지 않으면 None."""
    cur.execute(BUCKETS, (venue, MIN_BUCKET_N))
    rows = cur.fetchall()
    for bucket, _n, rate in rows:
        if rate >= 0.5:
            return float(bucket)
    return None


def main() -> None:
    with cursor() as cur:
        cur.execute(DDL)
        cur.execute(BASE)
        cur.execute(AGG)
        rows = cur.fetchall()

        out = []
        for (venue, papers, acc, rej, mean, sd, acc_mean, rej_mean, mx) in rows:
            decided = acc + rej
            reject_share = (rej / decided) if decided else 0.0
            biased = reject_share < MIN_REJECT_SHARE
            # 편향된 venue의 당락 경계는 reject 표본이 없어 추정 자체가 무의미하다
            threshold = None if biased else _threshold(cur, venue)
            row = (venue, papers, acc, rej,
                   round(acc / papers, 4) if papers else 0.0,
                   mean and round(float(mean), 3), sd and round(float(sd), 3),
                   acc_mean and round(float(acc_mean), 3),
                   rej_mean and round(float(rej_mean), 3),
                   mx and round(float(mx), 1), threshold, biased)
            cur.execute(UPSERT, row)
            out.append(row)

    print(f"\n{'venue':14}{'논문':>7}{'accept':>8}{'척도':>6}{'평균':>7}"
          f"{'accept평균':>11}{'reject평균':>11}{'경계':>7}  편향")
    print("-" * 78)
    for (venue, papers, acc, _rej, rate, mean, _sd, am, rm, mx, th, biased) in out:
        print(f"{venue:14}{papers:7,}{rate*100:7.1f}%{mx:6.0f}{mean:7.2f}"
              f"{am or 0:11.2f}{rm or 0:11.2f}"
              f"{(f'{th:.1f}' if th else '-'):>7}  {'⚠️ 예' if biased else '아니오'}")
    print(f"\n✅ {len(out)}개 venue 적재 완료")


if __name__ == "__main__":
    main()
