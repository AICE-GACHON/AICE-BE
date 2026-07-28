"""정규화 레이어 단위 테스트 (실제 API 응답 형태를 축약해 재현)."""
from paper_assistant.ingest.normalize import (
    clean_text, normalize_decision, normalize_paper, normalize_review, parse_score)


def test_clean_text_strips_nul_and_control_chars():
    # Postgres text는 NUL을 거부한다 (실측 ICLR 2021)
    assert clean_text("ab\x00cd") == "abcd"
    assert clean_text("x\x07y") == "xy"          # BEL 등 제어문자 제거
    assert clean_text("line1\nline2\ttab") == "line1\nline2\ttab"  # 탭·개행 보존
    assert clean_text("") == ""
    assert clean_text("정상 텍스트") == "정상 텍스트"       # 유니코드 보존


def test_get_field_cleans_nul():
    from paper_assistant.ingest.normalize import get_field
    assert get_field({"title": {"value": "Hel\x00lo"}}, ["title"]) == "Hello"


def test_parse_score_handles_all_observed_formats():
    assert parse_score("8: Accept") == 8.0
    assert parse_score("5") == 5.0
    assert parse_score("2 fair") == 2.0
    assert parse_score("3: reject, not good enough") == 3.0
    assert parse_score(7) == 7.0
    assert parse_score("") is None
    assert parse_score(None) is None


def test_normalize_decision_from_venue_string():
    assert normalize_decision("ICLR 2024 poster") == "accept-poster"
    assert normalize_decision("ICLR 2024 spotlight") == "accept-spotlight"
    assert normalize_decision("Submitted to ICLR 2024") == "reject"
    assert normalize_decision("ICLR 2024 Conference Withdrawn Submission") == "withdrawn"
    assert normalize_decision("ICLR 2024 Conference Desk Rejected Submission") == "desk-reject"


def test_normalize_decision_falls_back_to_decision_note():
    # ICLR 2020/2021은 submission에 venue 필드가 없다
    assert normalize_decision("", "Reject") == "reject"
    assert normalize_decision("", "Accept (Poster)") == "accept-poster"
    assert normalize_decision("", "") == "unknown"


def test_review_with_split_fields_needs_no_llm_split():
    """ICLR 2024/2025, NeurIPS 2023/2024 형식 (v2 래핑)."""
    note = {"id": "r1", "content": {
        "rating": {"value": "5"},
        "confidence": {"value": "4: You are confident..."},
        "summary": {"value": "A paper about X."},
        "strengths": {"value": "Well written."},
        "weaknesses": {"value": "Experiments limited to CIFAR-10."},
        "questions": {"value": "Why not ImageNet?"}}}
    r = normalize_review(note)
    assert r.rating == 5.0
    assert r.confidence == 4.0
    assert r.weaknesses == "Experiments limited to CIFAR-10."
    assert r.needs_llm_split is False
    assert r.llm_input == "Experiments limited to CIFAR-10."


def test_review_with_combined_field_needs_llm_split():
    """ICLR 2023 / NeurIPS 2022 형식 (강점+약점 합침, v1 raw)."""
    note = {"id": "r2", "content": {
        "recommendation": "5: marginally below the acceptance threshold",
        "summary_of_the_paper": "Proposes Y.",
        "strength_and_weaknesses": "Strength: novel. Weakness: no baselines."}}
    r = normalize_review(note)
    assert r.rating == 5.0
    assert r.needs_llm_split is True
    assert "no baselines" in r.weaknesses


def test_review_with_fulltext_only_needs_llm_split():
    """ICLR 2020/2021, NeurIPS 2021 형식 (통짜 리뷰)."""
    note = {"id": "r3", "content": {
        "rating": "8: Accept",
        "review": "This paper presents an improvement to transfer learning..."}}
    r = normalize_review(note)
    assert r.rating == 8.0
    assert r.needs_llm_split is True
    assert r.weaknesses.startswith("This paper presents")


def test_normalize_paper_collects_reviews_and_metareview():
    submission = {"id": "p1", "forum": "p1", "content": {
        "title": {"value": "Test Paper"},
        "abstract": {"value": "An abstract."},
        "keywords": {"value": ["ml", "nlp"]},
        "authors": {"value": ["Alice", "Bob"]},
        "authorids": {"value": ["~Alice_1", "~Bob_1"]},
        "venue": {"value": "ICLR 2024 poster"}}}
    replies = [
        {"id": "r1", "invitations": ["ICLR.cc/2024/Conference/-/Official_Review"],
         "content": {"rating": {"value": "6"}, "weaknesses": {"value": "Minor issues."}}},
        {"id": "m1", "invitations": ["ICLR.cc/2024/Conference/-/Meta_Review"],
         "content": {"metareview": {"value": "Reviewers agreed it is solid."}}},
        {"id": "d1", "invitations": ["ICLR.cc/2024/Conference/-/Decision"],
         "content": {"decision": {"value": "Accept (poster)"}}},
    ]
    p = normalize_paper(submission, replies, "ICLR 2024", 2024)
    assert p.title == "Test Paper"
    assert p.decision == "accept-poster"
    assert len(p.reviews) == 1
    assert p.meta_review == "Reviewers agreed it is solid."
    assert p.author_ids == ["~Alice_1", "~Bob_1"]


def test_metareview_falls_back_to_decision_comment():
    """ICLR 2020~2023, NeurIPS 2021/23/24는 Decision 노트에 메타리뷰가 들어있다."""
    submission = {"id": "p2", "forum": "p2", "content": {"title": "T", "abstract": "A"}}
    replies = [
        {"id": "d1", "invitation": "ICLR.cc/2021/Conference/-/Decision",
         "content": {"decision": "Accept (Poster)",
                     "comment": "The AC recommends acceptance."}},
    ]
    p = normalize_paper(submission, replies, "ICLR 2021", 2021)
    assert p.decision == "accept-poster"
    assert p.meta_review == "The AC recommends acceptance."


def test_v1_single_invitation_key_is_handled():
    """v1은 invitation(단수), v2는 invitations(복수)."""
    note = {"id": "r", "invitation": "ICLR.cc/2020/Conference/-/Official_Review",
            "content": {"rating": "6: Marginally above", "review": "Some text."}}
    p = normalize_paper({"id": "p", "forum": "p", "content": {"title": "T"}},
                        [note], "ICLR 2020", 2020)
    assert len(p.reviews) == 1
    assert p.reviews[0].rating == 6.0
