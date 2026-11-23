"""Minimal-support exact census expressions over a quadtree archive.

Model
-----
Every cell id is a node of a rooted 4-ary tree: the root is ``""`` and a child
appends one digit ``0``-``3``.  Leaf cells are the finest cells at
``MAX_DEPTH`` digits.

Given a *catalog* ``A`` (cell ids present in one archival batch, each with an
arbitrary approved population) and query cells ``Q``, the target region is the
union of leaves below the query cells.  An expression assigns an integer
coefficient ``c[a]`` to every catalog cell ``a``.  It is exact when, for **every**
population assignment to the finest cells,

    sum_a c[a] * pop(a) == pop(union of leaves below Q).

Populations are arbitrary, so this holds iff for every finest leaf ``l``

    sum_{a in A, a is ancestor-or-self of l} c[a] == 1[l in target].

The cost is the number of catalog cells with a non-zero coefficient, i.e. the
number of cells whose population is read.

Reduction
---------
Let ``X(v)`` be the running sum of coefficients from the root down to node
``v`` (including a catalog cell at ``v``).  Then ``c[v] = X(v) - X(parent)`` and
``X`` may change only at catalog cells; leaf constraints are ``X(l) = 1`` in the
target and ``0`` outside.  Clamping an integer labelling into ``[0, 1]`` never
creates a change (if ``X(v) == X(parent)`` the clamped values still agree) and
preserves leaf constraints, so an optimal solution exists with
``X(v) in {0, 1}`` and coefficients in ``{-1, 0, 1}``.

A two-state dynamic program over the trie of catalog/query prefixes therefore
yields the minimum number of retrieved cells.  Empty regions that contain no
catalog cell and no query are collapsed in O(1), so the solver never enumerates
the ``4**12`` finest cells: it only visits prefixes of submitted cells.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

MAX_DEPTH = 12
INF = 1 << 30
DIGITS = "0123"
MAX_MISSING_BLOCKS = 64
# Stop generating diagnostics once this many raw witnesses are found; the
# response only reports the shallowest MAX_MISSING_BLOCKS of them anyway.
_COLLECT_LIMIT = 4096


class _Node:
    __slots__ = (
        "cell",
        "depth",
        "children",
        "is_catalog",
        "is_query",
        "covered",
        "has_catalog",
    )

    def __init__(self, cell: str) -> None:
        self.cell = cell
        self.depth = len(cell)
        self.children: Dict[str, "_Node"] = {}
        self.is_catalog = False
        self.is_query = False
        # True when a strict ancestor is a query cell: the whole subtree lies
        # inside the target region.
        self.covered = False
        # True when this node or any descendant is a catalog cell.
        self.has_catalog = False


@dataclass(frozen=True)
class ExpressionTerm:
    cell: str
    coefficient: int


@dataclass(frozen=True)
class Solution:
    terms: Tuple[ExpressionTerm, ...]
    retrieved_cells: int

    def evaluate(self, population_of) -> int:
        return sum(t.coefficient * population_of(t.cell) for t in self.terms)

    def render(self) -> str:
        if not self.terms:
            return "0"
        parts: List[str] = []
        for term in self.terms:
            body = f'pop("{term.cell}")'
            if not parts:
                parts.append(body if term.coefficient > 0 else f"-{body}")
            else:
                parts.append(f"+ {body}" if term.coefficient > 0 else f"- {body}")
        return " ".join(parts)


@dataclass(frozen=True)
class Analysis:
    solution: Optional[Solution]
    missing_blocks: Tuple[str, ...] = ()
    missing_truncated: bool = False

    @property
    def feasible(self) -> bool:
        return self.solution is not None


def _build_trie(catalog: Iterable[str], queries: Iterable[str]) -> _Node:
    root = _Node("")
    for cell in catalog:
        node = root
        for digit in cell:
            node = node.children.setdefault(digit, _Node(node.cell + digit))
        node.is_catalog = True
    for cell in queries:
        node = root
        for digit in cell:
            node = node.children.setdefault(digit, _Node(node.cell + digit))
        node.is_query = True
    return root


def _prepare(root: _Node) -> None:
    """Mark covered subtrees and whether a subtree holds any catalog/query."""
    order: List[_Node] = []
    stack: List[Tuple[_Node, bool]] = [(root, False)]
    while stack:
        node, covered = stack.pop()
        node.covered = covered
        order.append(node)
        child_covered = covered or node.is_query
        for child in node.children.values():
            stack.append((child, child_covered))
    for node in reversed(order):
        node.has_catalog = node.is_catalog or any(
            child.has_catalog for child in node.children.values()
        )


def _cost(node: _Node, entering: int, depth: int, memo) -> int:
    """Minimum nonzero coefficients in this subtree under ``entering`` label."""
    key = (node, entering)
    cached = memo.get(key)
    if cached is not None:
        return cached[0]

    # Requirement shared by every finest leaf below this node (before deeper
    # query cells are considered only if they exist in the trie).
    child_covered = node.covered or node.is_query

    if node.depth == depth:
        # A query cell at the finest level: its single leaf must be 1.
        target = 1 if child_covered else 0
        if node.is_catalog:
            cost = 0 if entering == target else 1
            chosen = target
        else:
            cost = 0 if entering == target else INF
            chosen = entering
        memo[key] = (cost, chosen)
        return cost

    def child_cost(digit: str, label: int) -> int:
        child = node.children.get(digit)
        if child is not None:
            return _cost(child, label, depth, memo)
        # Virtual child: no catalog cell and no query below.  Every leaf there
        # must equal the covered flag, which is uniform.
        required = 1 if child_covered else 0
        return 0 if label == required else INF

    labels = (entering, 1 - entering) if node.is_catalog else (entering,)

    best = INF
    best_label = entering
    for label in labels:
        cost = 0 if label == entering else 1
        if cost >= best:
            continue
        for digit in DIGITS:
            cost += child_cost(digit, label)
            if cost >= best:
                break
        if cost < best:
            best = cost
            best_label = label

    memo[key] = (best, best_label)
    return best


def _feasible(node: _Node, entering: int, depth: int, memo) -> bool:
    """Whether the subtree can satisfy every leaf requirement (cost ignored)."""
    key = (node, entering)
    cached = memo.get(key)
    if cached is not None:
        return cached
    child_covered = node.covered or node.is_query
    required = 1 if child_covered else 0

    if node.depth == depth:
        if node.is_catalog:
            result = True  # the leaf can adopt either label at unit cost
        else:
            result = entering == required
        memo[key] = result
        return result

    def child_ok(digit: str, label: int) -> bool:
        child = node.children.get(digit)
        if child is not None:
            return _feasible(child, label, depth, memo)
        return label == required

    labels = (entering, 1 - entering) if node.is_catalog else (entering,)
    result = any(all(child_ok(digit, label) for digit in DIGITS) for label in labels)
    memo[key] = result
    return result


def _collect_missing(node: _Node, entering: int, depth: int, memo, out: List[str]) -> None:
    """Report the smallest cell regions that the archive cannot supply."""
    if len(out) >= _COLLECT_LIMIT:
        return
    if _feasible(node, entering, depth, memo):
        return
    if node.depth == depth:
        if not node.is_catalog:
            out.append(node.cell)
        return  # a catalog leaf can always adopt the needed label
    if not node.has_catalog:
        out.append(node.cell)
        return

    child_covered = node.covered or node.is_query
    required = 1 if child_covered else 0
    labels = (entering, 1 - entering) if node.is_catalog else (entering,)

    best_branch: Optional[List[str]] = None
    for label in labels:
        if all(
            (child is not None and _feasible(child, label, depth, memo))
            or (child is None and label == required)
            for digit in DIGITS
            for child in (node.children.get(digit),)
        ):
            continue  # this label already works; the node would be feasible
        branch: List[str] = []
        for digit in DIGITS:
            child = node.children.get(digit)
            if child is None:
                if label != required:
                    branch.append(node.cell + digit)
            elif not _feasible(child, label, depth, memo):
                _collect_missing(child, label, depth, memo, branch)
        if best_branch is None or len(branch) < len(best_branch):
            best_branch = branch
    if best_branch:
        out.extend(best_branch)


def _prune_blocks(blocks: Iterable[str]) -> Tuple[List[str], bool]:
    ordered = sorted(set(blocks), key=lambda cell: (len(cell), cell))
    kept: List[str] = []
    for cell in ordered:
        if any(cell.startswith(parent) for parent in kept):
            continue
        kept.append(cell)
    truncated = len(kept) > MAX_MISSING_BLOCKS
    return kept[:MAX_MISSING_BLOCKS], truncated


def analyze(
    catalog: Iterable[str],
    queries: Sequence[str],
    depth: int = MAX_DEPTH,
) -> Analysis:
    """Find a minimum-support exact expression, or explain the missing archive.

    Only catalog cells, query cells and their prefixes are visited.  No finest
    cell is enumerated and no population value is required.
    """
    catalog = tuple(catalog)
    queries = tuple(queries)
    for label, cells in (("catalog", catalog), ("query", queries)):
        for cell in cells:
            if not isinstance(cell, str):
                raise ValueError(f"{label} cell id must be a string")
            if len(cell) > depth:
                raise ValueError(
                    f"{label} cell {cell!r} exceeds maximum grid depth {depth}"
                )
            if any(digit not in DIGITS for digit in cell):
                raise ValueError(
                    f"{label} cell {cell!r} contains a digit outside 0-3"
                )

    if not queries:
        # The union of an empty set of cells is empty.
        return Analysis(solution=Solution(terms=(), retrieved_cells=0))

    root = _build_trie(catalog, queries)
    _prepare(root)
    memo: Dict[Tuple[_Node, int], Tuple[int, int]] = {}
    cost = _cost(root, 0, depth, memo)
    if cost >= INF:
        raw: List[str] = []
        _collect_missing(root, 0, depth, {}, raw)
        blocks, truncated = _prune_blocks(raw)
        return Analysis(None, tuple(blocks), truncated)

    terms: List[ExpressionTerm] = []

    def walk(node: _Node, entering: int) -> None:
        chosen = memo[(node, entering)][1]
        if node.is_catalog and chosen != entering:
            terms.append(ExpressionTerm(cell=node.cell, coefficient=chosen - entering))
        if node.depth == depth:
            return
        for digit in DIGITS:
            child = node.children.get(digit)
            if child is not None:
                walk(child, chosen)

    walk(root, 0)
    terms.sort(key=lambda term: (len(term.cell), term.cell))
    return Analysis(Solution(terms=tuple(terms), retrieved_cells=len(terms)))


def solve(
    catalog: Iterable[str],
    queries: Sequence[str],
    depth: int = MAX_DEPTH,
) -> Optional[Solution]:
    """Return a minimum-support exact expression, or ``None`` if impossible."""
    return analyze(catalog, queries, depth=depth).solution


def target_leaves(queries: Iterable[str], depth: int = MAX_DEPTH) -> frozenset:
    """Reference helper for tests: the exact set of target finest cells."""
    leaves = [""]
    for _ in range(depth):
        leaves = [leaf + digit for leaf in leaves for digit in DIGITS]
    queries = tuple(queries)
    return frozenset(
        leaf for leaf in leaves if any(leaf.startswith(query) for query in queries)
    )
