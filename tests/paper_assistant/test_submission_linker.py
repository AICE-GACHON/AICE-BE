"""재투고 매칭 순수 로직 테스트 (DB 불필요)."""
from paper_assistant.ingest.submission_linker import (
    author_jaccard, normalize_title, venue_sort_key, _order,
    _different_submission)


def test_normalize_title_strips_punctuation_and_case():
    assert normalize_title("Deep Learning!") == "deep learning"
    assert normalize_title("A Method: For X (v2)") == "a method for x v2"
    assert (normalize_title("Attention Is All You Need") ==
            normalize_title("attention   is all-you-need"))


def test_normalize_title_handles_empty():
    assert normalize_title("") == ""
    assert normalize_title(None) == ""


def test_venue_sort_key_iclr_before_neurips_same_year():
    assert venue_sort_key("ICLR 2024", 2024) < venue_sort_key("NeurIPS 2024", 2024)


def test_venue_sort_key_earlier_year_first():
    assert venue_sort_key("NeurIPS 2023", 2023) < venue_sort_key("ICLR 2024", 2024)


def test_order_puts_earlier_submission_first():
    iclr = {"id": 10, "venue": "ICLR 2024", "year": 2024}
    neurips = {"id": 20, "venue": "NeurIPS 2024", "year": 2024}
    # ICLR 2024 → NeurIPS 2024 흐름: earlier=ICLR(10)
    assert _order(neurips, iclr) == (10, 20)
    assert _order(iclr, neurips) == (10, 20)


def test_author_jaccard():
    assert author_jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert author_jaccard({"a", "b", "c", "d"}, {"a", "b"}) == 0.5
    assert author_jaccard({"a"}, {"b"}) == 0.0
    assert author_jaccard(set(), {"a"}) == 0.0


def test_different_submission_rejects_same_venue_year():
    p1 = {"venue": "ICLR 2024", "year": 2024}
    p2 = {"venue": "ICLR 2024", "year": 2024}
    p3 = {"venue": "NeurIPS 2024", "year": 2024}
    assert not _different_submission(p1, p2)   # 같은 투고 — 재투고 아님
    assert _different_submission(p1, p3)
