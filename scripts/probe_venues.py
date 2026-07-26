"""수집 대상 venue×연도별 스키마 차이를 조사한다.

각 venue에서 논문 1편 + 그 리뷰를 가져와 필드명/rating 형식을 비교.
설계서 §9의 최대 리스크(연도별 필드 구조 상이) 검증용.
"""
import json

from paper_assistant import config
from paper_assistant.ingest.openreview_client import OpenReviewClient

VENUES = [
    "ICLR.cc/2020/Conference",
    "ICLR.cc/2021/Conference",
    "ICLR.cc/2022/Conference",
    "ICLR.cc/2023/Conference",
    "ICLR.cc/2024/Conference",
    "ICLR.cc/2025/Conference",
    "NeurIPS.cc/2021/Conference",
    "NeurIPS.cc/2022/Conference",
    "NeurIPS.cc/2023/Conference",
    "NeurIPS.cc/2024/Conference",
]

# venue별로 제출 invitation 이름이 다름 (구버전은 Blind_Submission)
SUBMISSION_INVITATIONS = ["-/Submission", "-/Blind_Submission"]


def v(f):
    return f.get("value") if isinstance(f, dict) else f


def probe(client, venue):
    result = {"venue": venue}

    sub = None
    for inv_suffix in SUBMISSION_INVITATIONS:
        try:
            for note in client.iter_notes(invitation=f"{venue}/{inv_suffix}"):
                sub = note
                result["submission_invitation"] = inv_suffix
                break
        except Exception as e:
            result.setdefault("errors", []).append(f"{inv_suffix}: {type(e).__name__}")
        if sub:
            break

    if not sub:
        result["status"] = "제출 논문 조회 실패"
        return result

    result["status"] = "ok"
    result["submission_keys"] = sorted(sub["content"].keys())
    result["venue_value"] = v(sub["content"].get("venue", {}))
    result["has_abstract"] = "abstract" in sub["content"]

    try:
        replies = client.get_forum_replies(sub["forum"])
    except Exception as e:
        result["reply_error"] = f"{type(e).__name__}: {e}"
        return result

    kinds = {}
    for rep in replies:
        for inv in rep.get("invitations", []) or [rep.get("invitation")]:
            if not inv:
                continue
            kinds.setdefault(inv.split("/")[-1], rep)
    result["reply_kinds"] = sorted(kinds.keys())

    for kind in kinds:
        if "Review" in kind and "Meta" not in kind:
            c = kinds[kind]["content"]
            result["review_kind"] = kind
            result["review_keys"] = sorted(c.keys())
            result["rating_sample"] = str(v(c.get("rating", c.get("recommendation", ""))))[:60]
            # 지적 내용이 별도 필드로 분리되어 있는지 (LLM 추출 필요 여부의 핵심)
            result["has_split_fields"] = [
                k for k in ("strengths", "weaknesses", "questions",
                            "strength_and_weaknesses", "main_review", "review")
                if k in c]
            break
    return result


def main():
    client = OpenReviewClient()
    results = []
    for venue in VENUES:
        try:
            r = probe(client, venue)
        except Exception as e:
            r = {"venue": venue, "status": f"실패: {type(e).__name__}: {e}"}
        results.append(r)
        print(f"\n--- {venue} [{r.get('status')}] ---")
        if r.get("status") == "ok":
            print(f"  submission inv : {r.get('submission_invitation')}")
            print(f"  venue 값       : {r.get('venue_value')}")
            print(f"  리플라이 유형   : {r.get('reply_kinds')}")
            print(f"  리뷰 종류      : {r.get('review_kind')}")
            print(f"  리뷰 필드      : {r.get('review_keys')}")
            print(f"  rating 예시    : {r.get('rating_sample')}")
            print(f"  분리 필드 존재  : {r.get('has_split_fields')}")
        else:
            print(f"  {r}")

    out = config.RAW_DIR / "venue_schema_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n저장: {out}")


if __name__ == "__main__":
    main()
