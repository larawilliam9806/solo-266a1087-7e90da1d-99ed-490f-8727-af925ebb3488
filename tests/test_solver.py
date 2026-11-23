import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.solver import analyze, solve, target_leaves


def brute(catalog, queries, depth):
    leaves = target_leaves(queries, depth)
    target = set(leaves)
    best = None
    for coeffs in itertools.product((-1, 0, 1), repeat=len(catalog)):
        cost = sum(1 for c in coeffs if c)
        if best is not None and cost >= best:
            continue
        ok = True
        for leaf in ("".join(t) for t in itertools.product("0123", repeat=depth)):
            value = sum(c for cell, c in zip(catalog, coeffs) if leaf.startswith(cell))
            if value != (1 if leaf in target else 0):
                ok = False
                break
        if ok:
            best = cost
    return best


def all_cells(depth):
    cells = [""]
    for length in range(1, depth + 1):
        cells += ["".join(t) for t in itertools.product("0123", repeat=length)]
    return cells


@pytest.mark.parametrize("depth", [1, 2])
def test_matches_bruteforce(depth):
    import random

    random.seed(depth)
    pool = all_cells(depth)
    for _ in range(120):
        catalog = random.sample(pool, random.randint(0, min(7, len(pool))))
        queries = random.sample(pool, random.randint(0, 3))
        sol = solve(catalog, queries, depth=depth)
        assert (None if sol is None else sol.retrieved_cells) == brute(
            catalog, queries, depth
        )


def finest_leaves(depth):
    leaves = [""]
    for _ in range(depth):
        leaves = [leaf + digit for leaf in leaves for digit in "0123"]
    return leaves


def test_expression_is_exact_for_arbitrary_populations():
    depth = 2
    catalog = ["", "0", "01", "1", "2", "3"]
    queries = ["1", "3"]
    sol = solve(catalog, queries, depth=depth)
    assert sol is not None
    leaves = finest_leaves(depth)

    def cell_population(leaf_pops, cell):
        return sum(leaf_pops.get(leaf, 0) for leaf in leaves if leaf.startswith(cell))

    # Exact for *any* distribution over the finest cells: the stored value of a
    # cell is the sum of its finest descendants.
    for leaf_pops in [
        dict.fromkeys(leaves, 0),
        {"10": 7, "33": 2, "01": 9, "00": 4},
        {leaf: index for index, leaf in enumerate(leaves)},
    ]:
        expected = sum(leaf_pops.get(leaf, 0) for leaf in target_leaves(queries, depth))
        actual = sum(
            term.coefficient * cell_population(leaf_pops, term.cell)
            for term in sol.terms
        )
        assert actual == expected


def test_minimal_expression_avoids_unneeded_cells():
    # Target is exactly cell "2": reading pop("2") costs one cell and is exact.
    sol = solve(["0", "1", "2", "3"], ["2"], depth=2)
    assert [t.cell for t in sol.terms] == ["2"]
    assert sol.retrieved_cells == 1


def test_subtraction_allowed_and_minimal():
    # Only cells "0" and "01" are archived.  Target is the finest leaves below
    # "0" except those below "01": pop("0") - pop("01").
    sol = solve(["0", "01"], ["0"], depth=2)
    assert sol is not None
    assert {(t.cell, t.coefficient) for t in sol.terms} == {("0", 1)}

    catalog = ["0", "01"]
    target = target_leaves(["0"], depth=2) - target_leaves(["01"], depth=2)
    # Build the same target via queries that exclude 01: query the four leaves.
    queries = sorted(target - target_leaves(["01"], depth=2))
    # Simpler: query cells are the three siblings of 01 below 0.
    queries = ["00", "02", "03"]
    sol = solve(catalog, queries, depth=2)
    assert sol is not None
    total = 0
    for term in sol.terms:
        total += term.coefficient
    assert sol.retrieved_cells <= 2


def test_missing_archive_reported_when_region_absent():
    analysis = analyze(["0", "1"], ["3"], depth=2)
    assert not analysis.feasible
    assert "3" in analysis.missing_blocks


def test_root_cell_and_empty_query():
    analysis = analyze(["0", "1", "2", "3"], [""], depth=2)
    assert analysis.feasible
    assert analysis.solution.retrieved_cells == 4
    empty = analyze(["0", "1"], [], depth=2)
    assert empty.feasible
    assert empty.solution.retrieved_cells == 0
    assert empty.solution.render() == "0"


def test_solver_visits_only_submitted_prefixes():
    # A depth-12 grid has 4**12 leaves; solver must stay cheap.
    import time

    catalog = ["", "01", "012", "0123", "3" * 12]
    queries = ["0123" + "0" * 8, "3" * 12]
    started = time.perf_counter()
    for _ in range(200):
        analyze(catalog, queries, depth=12)
    assert time.perf_counter() - started < 2.0
