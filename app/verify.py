"""Consistency checks between ancestor and descendant headcounts.

A batch stores one headcount per quadtree cell.  For the archive to supply
sound numbers, every stored cell must agree with the stored cells below it:

* the check of an ancestor reads its **shallowest** stored descendants --
  finest cells already covered by another stored descendant are skipped, so
  nested cells are never counted twice;
* the descendants' total must not exceed the ancestor's own headcount;
* when the descendants **tile** the ancestor (every finest cell below it is
  covered), their total must equal the ancestor's headcount exactly.

``find_conflicts`` reports every ancestor that violates either rule, in a
stable order, together with the cells that participated in the check.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

EXCEEDS = "exceeds"
MISMATCH = "mismatch"


@dataclass(frozen=True)
class Conflict:
    """One ancestor whose stored descendants contradict its headcount."""

    ancestor: str
    ancestor_population: int
    # The shallowest stored descendants that joined the check, sorted.
    cells: Tuple[str, ...]
    descendant_total: int
    kind: str  # EXCEEDS or MISMATCH


def _nearest_stored_ancestor(cell: str, stored: Set[str]) -> Optional[str]:
    for end in range(len(cell) - 1, -1, -1):
        prefix = cell[:end]
        if prefix in stored:
            return prefix
    return None


def _tiling_masks(stored: Set[str]) -> Dict[str, int]:
    """For every cell, which child subtrees are fully covered by stored cells.

    Bit ``d`` of ``masks[c]`` is set when every finest cell below ``c + d``
    has a stored cell on the path strictly below ``c``.  A stored cell covers
    its own subtree; a non-stored cell is covered once all four children are.
    """
    masks: Dict[str, int] = {}
    covered: Set[str] = set(stored)
    queue: deque = deque(sorted(stored, key=len, reverse=True))
    while queue:
        cell = queue.popleft()
        if not cell:
            continue
        parent, digit = cell[:-1], cell[-1]
        bit = 1 << int(digit)
        mask = masks.get(parent, 0)
        if mask & bit:
            continue
        mask |= bit
        masks[parent] = mask
        if mask == 0b1111 and parent not in covered:
            covered.add(parent)
            queue.append(parent)
    return masks


def find_conflicts(populations: Mapping[str, int]) -> Tuple[Conflict, ...]:
    """Return every ancestor/descendant contradiction in one batch.

    Only the mapping of stored cells to headcounts is inspected; no finest
    cell is ever enumerated.
    """
    stored: Set[str] = set(populations)
    # Group every stored cell under its nearest stored strict ancestor: those
    # are exactly the shallowest descendants each ancestor is checked against.
    children: Dict[str, List[str]] = {}
    for cell in sorted(stored, key=lambda c: (len(c), c)):
        ancestor = _nearest_stored_ancestor(cell, stored)
        if ancestor is not None:
            children.setdefault(ancestor, []).append(cell)

    masks = _tiling_masks(stored)
    conflicts: List[Conflict] = []
    for ancestor in sorted(children, key=lambda c: (len(c), c)):
        cells = tuple(sorted(children[ancestor], key=lambda c: (len(c), c)))
        total = sum(populations[cell] for cell in cells)
        own = populations[ancestor]
        if total > own:
            kind = EXCEEDS
        elif masks.get(ancestor, 0) == 0b1111 and total != own:
            kind = MISMATCH
        else:
            continue
        conflicts.append(
            Conflict(
                ancestor=ancestor,
                ancestor_population=own,
                cells=cells,
                descendant_total=total,
                kind=kind,
            )
        )
    return tuple(conflicts)


def conflict_to_dict(conflict: Conflict) -> Dict[str, object]:
    return {
        "ancestor": conflict.ancestor,
        "ancestor_population": conflict.ancestor_population,
        "cells": list(conflict.cells),
        "descendant_total": conflict.descendant_total,
        "kind": conflict.kind,
    }


def conflicts_to_json(conflicts: Sequence[Conflict]) -> str:
    return json.dumps([conflict_to_dict(c) for c in conflicts])


def conflicts_from_json(payload: str) -> Tuple[Conflict, ...]:
    return tuple(
        Conflict(
            ancestor=item["ancestor"],
            ancestor_population=item["ancestor_population"],
            cells=tuple(item["cells"]),
            descendant_total=item["descendant_total"],
            kind=item["kind"],
        )
        for item in json.loads(payload)
    )
