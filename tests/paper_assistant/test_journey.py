"""재투고 궤적의 순수 로직 테스트 (DB 불필요).

여기서 고정하는 건 실측에서 나온 사항이다 — submission_links는 인접 쌍만
기록하고(8346→174, 174→27997 같은 사슬이 존재), 같은 해 안에서 옮겨간 투고가
있어(ICLR 2024 → NeurIPS 2024) 연도만으로는 순서가 안 갈린다.
"""
from paper_assistant.query.journey import _order, _outcome
from paper_assistant.schemas import JourneyStop


def stop(venue, decision, year=2024, pid=1):
    return JourneyStop(paper_id=pid, openreview_id=f"or{pid}", title="t",
                       venue=venue, year=year, decision=decision)


# ---------------------------------------------------------------- _order

def test_order_follows_link_direction_when_years_tie():
    # ICLR 2024 -> NeurIPS 2024. 연도가 같아 링크 방향이 유일한 근거다.
    years = {121: 2024, 40190: 2024}
    assert _order({121, 40190}, [(121, 40190, "title_exact", 0.95)], years) == \
        [121, 40190]


def test_order_reconstructs_full_chain_from_the_middle():
    # 실측 사슬. 가운데(174)에서 조회해도 앞뒤가 모두 붙어야 한다.
    years = {8346: 2023, 174: 2024, 27997: 2025}
    edges = [(8346, 174, "title_exact", 0.95), (174, 27997, "title_exact", 0.95)]
    assert _order({8346, 174, 27997}, edges, years) == [8346, 174, 27997]


def test_order_is_stable_regardless_of_edge_input_order():
    years = {8346: 2023, 174: 2024, 27997: 2025}
    edges = [(174, 27997, "title_exact", 0.95), (8346, 174, "title_exact", 0.95)]
    assert _order({8346, 174, 27997}, edges, years) == [8346, 174, 27997]


def test_order_keeps_papers_that_form_a_cycle():
    # 양방향으로 잘못 매칭되면 위상 정렬이 아무것도 못 내놓는다. 논문을 버리지
    # 않고 연도순으로라도 돌려줘야 궤적이 통째로 사라지지 않는다.
    years = {1: 2023, 2: 2024}
    edges = [(1, 2, "title_exact", 0.9), (2, 1, "title_exact", 0.9)]
    assert _order({1, 2}, edges, years) == [1, 2]


def test_order_handles_paper_with_no_links():
    assert _order({7}, [], {7: 2024}) == [7]


# -------------------------------------------------------------- _outcome

def test_outcome_single_has_no_message():
    outcome, message = _outcome([stop("ICLR 2024", "reject")])
    assert outcome == "single"
    assert message is None


def test_outcome_marks_reject_then_accept_as_improved():
    outcome, message = _outcome([
        stop("ICLR 2024", "reject", 2024, 121),
        stop("NeurIPS 2024", "accept-poster", 2024, 40190)])
    assert outcome == "improved"
    assert "NeurIPS 2024" in message


def test_outcome_counts_withdrawn_as_not_accepted():
    # 철회는 통과가 아니다. accept로 끝나지 않았으면 improved가 되면 안 된다.
    outcome, _ = _outcome([
        stop("ICLR 2024", "reject", 2024, 1),
        stop("ICLR 2025", "withdrawn", 2025, 2)])
    assert outcome == "still_rejected"


def test_outcome_repeated_rejection_lists_the_venues():
    outcome, message = _outcome([
        stop("ICLR 2023", "reject", 2023, 8346),
        stop("ICLR 2024", "reject", 2024, 174),
        stop("ICLR 2025", "reject", 2025, 27997)])
    assert outcome == "still_rejected"
    assert "ICLR 2023 → ICLR 2024 → ICLR 2025" in message


def test_outcome_accept_then_later_submission_is_mixed():
    outcome, _ = _outcome([
        stop("ICLR 2024", "accept-poster", 2024, 1),
        stop("ICLR 2025", "reject", 2025, 2)])
    assert outcome == "mixed"
