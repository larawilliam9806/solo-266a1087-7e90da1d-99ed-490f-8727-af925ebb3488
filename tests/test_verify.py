import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.verify import find_conflicts


def test_empty_batch_has_no_conflicts():
    assert find_conflicts({}) == ()


def test_single_cell_has_no_conflicts():
    assert find_conflicts({"": 100}) == ()
    assert find_conflicts({"0123": 5}) == ()


def test_descendant_exceeding_ancestor_is_reported():
    # The motivating bug: "0" records 10 but "01" records 12.
    (conflict,) = find_conflicts({"0": 10, "01": 12})
    assert conflict.ancestor == "0"
    assert conflict.ancestor_population == 10
    assert conflict.cells == ("01",)
    assert conflict.descendant_total == 12
    assert conflict.kind == "exceeds"


def test_partial_coverage_below_total_is_allowed():
    assert find_conflicts({"0": 10, "01": 4}) == ()
    assert find_conflicts({"0": 10, "01": 10}) == ()


def test_cells_covered_by_another_descendant_are_skipped():
    # Checking "0" reads only "01"; "010" is already covered by "01".
    assert find_conflicts({"0": 10, "01": 6, "010": 6}) == ()
    # ...but "010" is still checked against its own ancestor "01".
    (conflict,) = find_conflicts({"0": 10, "01": 6, "010": 7})
    assert conflict.ancestor == "01"
    assert conflict.cells == ("010",)
    assert conflict.kind == "exceeds"


def test_tiled_ancestor_must_match_total():
    assert find_conflicts({"0": 10, "00": 2, "01": 2, "02": 2, "03": 4}) == ()
    (conflict,) = find_conflicts({"0": 10, "00": 2, "01": 2, "02": 2, "03": 2})
    assert conflict.ancestor == "0"
    assert conflict.descendant_total == 8
    assert conflict.kind == "mismatch"


def test_tiling_may_run_through_unstored_cells():
    # "000" and "001" are direct descendants of "0" in the stored forest
    # because "00" itself is not archived; coverage of "00" is incomplete.
    pops = {"0": 10, "000": 3, "001": 3, "01": 2, "02": 1, "03": 1}
    assert find_conflicts(pops) == ()
    # Archiving "00" as well tiles "0" only if every quarter is covered.
    pops = {"0": 10, "00": 6, "000": 3, "001": 3, "01": 2, "02": 1, "03": 1}
    assert find_conflicts(pops) == ()
    pops["000"] = 4  # "00"'s children now total 7 > 6
    (conflict,) = find_conflicts(pops)
    assert conflict.ancestor == "00"
    assert conflict.kind == "exceeds"


def test_root_cell_is_checked():
    (conflict,) = find_conflicts({"": 100, "0": 20, "1": 20, "2": 20, "3": 20})
    assert conflict.ancestor == ""
    assert conflict.kind == "mismatch"
    assert find_conflicts({"": 100, "0": 25, "1": 25, "2": 25, "3": 25}) == ()


def test_multiple_conflicts_are_all_reported_in_stable_order():
    conflicts = find_conflicts({"0": 1, "00": 5, "1": 2, "10": 3})
    assert [c.ancestor for c in conflicts] == ["0", "1"]
    assert all(c.kind == "exceeds" for c in conflicts)


def test_deepest_level_cells():
    parent = "01230123012"  # 11 digits
    child = parent + "3"  # 12 digits, the finest grid
    assert find_conflicts({parent: 5, child: 5}) == ()
    (conflict,) = find_conflicts({parent: 4, child: 5})
    assert conflict.ancestor == parent
    assert conflict.cells == (child,)
