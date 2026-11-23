"""SQLite archive storage for census batches.

Each statistical period owns exactly one batch.  A batch is stored in a single
SQLite transaction; replacing a batch deletes the previous rows and inserts the
new ones atomically, so a rejected submission leaves the previous batch intact.

Every batch is checked for ancestor/descendant headcount consistency (see
``app.verify``) before it is stored, and the conclusion is saved per batch in
``verifications``, keyed by the batch's unique ``batch_id``.  Batches stored
before this rule existed carry no conclusion; they are verified lazily on
their first read and the conclusion is then saved under their own batch id,
so a late verification can never overwrite a newer batch's conclusion.

Schema
------
``batches(period TEXT PRIMARY KEY, batch_id TEXT NOT NULL, created_at TEXT NOT NULL)``
``cells(period TEXT, cell TEXT, population INTEGER, PRIMARY KEY(period, cell))``
``verifications(batch_id TEXT PRIMARY KEY, period TEXT, status TEXT, conflicts TEXT, verified_at TEXT)``

``cell`` is a quadtree id (root ``""``, children append ``0``-``3``); an empty
id is legal and stored as the empty string.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .verify import (
    Conflict,
    conflicts_from_json,
    conflicts_to_json,
    find_conflicts,
)

MAX_DEPTH = 12
DIGITS = "0123"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    period     TEXT PRIMARY KEY,
    batch_id   TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cells (
    period     TEXT    NOT NULL,
    cell       TEXT    NOT NULL,
    population INTEGER NOT NULL,
    PRIMARY KEY (period, cell),
    FOREIGN KEY (period) REFERENCES batches(period) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS cells_by_period ON cells(period);
CREATE TABLE IF NOT EXISTS verifications (
    batch_id    TEXT PRIMARY KEY,
    period      TEXT NOT NULL,
    status      TEXT NOT NULL,
    conflicts   TEXT NOT NULL,
    verified_at TEXT NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a pre-verification database forward.

    Runs inside the write transaction (``BEGIN IMMEDIATE``), so concurrent
    connections cannot race the ALTER.  Existing batches predate consistency
    checks: they receive a unique batch id but no conclusion, which marks
    them for verification on first read.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(batches)")}
    if "batch_id" not in columns:
        conn.execute("ALTER TABLE batches ADD COLUMN batch_id TEXT")
        for row in conn.execute("SELECT period FROM batches").fetchall():
            conn.execute(
                "UPDATE batches SET batch_id = ? WHERE period = ?",
                (uuid.uuid4().hex, row["period"]),
            )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS batches_by_id ON batches(batch_id)"
    )


class ArchiveError(Exception):
    """Base class for archive-level failures."""


class UnknownPeriod(ArchiveError):
    def __init__(self, period: str) -> None:
        super().__init__(f"no archive batch for period {period!r}")
        self.period = period


class BatchConflict(ArchiveError):
    def __init__(self, period: str) -> None:
        super().__init__(f"archive batch for period {period!r} already exists")
        self.period = period


class InvalidBatch(ArchiveError):
    pass


class InconsistentBatch(InvalidBatch):
    """A batch whose ancestor and descendant headcounts contradict."""

    def __init__(self, period: str, conflicts: Sequence[Conflict]) -> None:
        first = conflicts[0]
        super().__init__(
            f"batch for period {period!r} is inconsistent: the stored "
            f"descendants of cell {first.ancestor!r} total "
            f"{first.descendant_total} but the cell records "
            f"{first.ancestor_population}"
        )
        self.period = period
        self.conflicts = tuple(conflicts)


class MissingCells(ArchiveError):
    def __init__(self, period: str, cells: Sequence[str]) -> None:
        super().__init__(
            f"cells {list(cells)[:5]!r} are absent from period {period!r}"
        )
        self.period = period
        self.cells = tuple(cells)


def validate_cell(cell: str, depth: int = MAX_DEPTH) -> str:
    if not isinstance(cell, str):
        raise InvalidBatch("cell id must be a string")
    if len(cell) > depth:
        raise InvalidBatch(
            f"cell id {cell!r} has {len(cell)} digits, maximum is {depth}"
        )
    if any(digit not in DIGITS for digit in cell):
        raise InvalidBatch(f"cell id {cell!r} contains a digit outside 0-3")
    return cell


@dataclass(frozen=True)
class BatchInfo:
    period: str
    cell_count: int
    total_population: int
    created_at: str


@dataclass(frozen=True)
class VerificationRecord:
    """The saved consistency conclusion of one batch."""

    batch_id: str
    period: str
    status: str  # "ok" or "conflict"
    conflicts: Tuple[Conflict, ...]
    verified_at: str


class Archive:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # Create/migrate the schema and switch to WAL up front, so a later
        # read snapshot never blocks the writer that records a verification
        # or replaces a batch.
        with self._connect(write=True):
            pass

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            if write:
                # WAL is persisted in the database file; readers then see a
                # consistent snapshot without blocking the writer.
                conn.execute("PRAGMA journal_mode = WAL")
            # DDL is idempotent and must run outside an explicit transaction.
            conn.executescript(_SCHEMA)
            # BEGIN IMMEDIATE serializes writers, so the existence check and
            # the batch swap cannot interleave with a concurrent submission.
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if write:
                _migrate(conn)
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def replace_batch(
        self,
        period: str,
        entries: Sequence[Tuple[str, int]],
        *,
        allow_replace: bool = False,
    ) -> BatchInfo:
        """Atomically store a whole batch for ``period``.

        The batch is first checked for internal consistency: for every stored
        cell, its shallowest stored descendants must not total more than the
        cell's own headcount, and must equal it when they tile the cell.
        An inconsistent batch raises :class:`InconsistentBatch` before
        anything is written, so the previous batch and its verification
        conclusion stay untouched.  Duplicate cell ids in ``entries`` are
        rejected.  When ``allow_replace`` is false an existing batch raises
        :class:`BatchConflict`; in either case the operation is a single
        transaction, so failures keep the old batch.
        """
        if not period:
            raise InvalidBatch("statistical period must be a non-empty string")
        seen: Dict[str, int] = {}
        for cell, population in entries:
            validate_cell(cell)
            if cell in seen:
                raise InvalidBatch(f"duplicate cell id {cell!r} in batch")
            if isinstance(population, bool) or not isinstance(population, int):
                raise InvalidBatch(f"population for cell {cell!r} must be an integer")
            if population < 0:
                raise InvalidBatch(f"population for cell {cell!r} must be non-negative")
            seen[cell] = population

        conflicts = find_conflicts(seen)
        if conflicts:
            raise InconsistentBatch(period, conflicts)

        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        batch_id = uuid.uuid4().hex
        with self._connect(write=True) as conn:
            existing = conn.execute(
                "SELECT 1 FROM batches WHERE period = ?", (period,)
            ).fetchone()
            if existing is not None and not allow_replace:
                raise BatchConflict(period)
            # One transaction: delete, upsert batch row, insert new cells and
            # the conclusion the batch was verified with.
            conn.execute("DELETE FROM cells WHERE period = ?", (period,))
            conn.execute(
                "INSERT INTO batches(period, batch_id, created_at) VALUES(?, ?, ?) "
                "ON CONFLICT(period) DO UPDATE SET batch_id = excluded.batch_id, "
                "created_at = excluded.created_at",
                (period, batch_id, created_at),
            )
            conn.executemany(
                "INSERT INTO cells(period, cell, population) VALUES(?, ?, ?)",
                [(period, cell, pop) for cell, pop in seen.items()],
            )
            conn.execute(
                "INSERT INTO verifications(batch_id, period, status, conflicts, "
                "verified_at) VALUES(?, ?, 'ok', '[]', ?)",
                (batch_id, period, created_at),
            )
        return BatchInfo(
            period=period,
            cell_count=len(seen),
            total_population=sum(seen.values()),
            created_at=created_at,
        )

    def record_verification(
        self, batch_id: str, period: str, conflicts: Sequence[Conflict]
    ) -> VerificationRecord:
        """Persist the verification conclusion of one batch, keyed by batch id.

        ``INSERT OR IGNORE`` keeps the first conclusion.  A verification that
        finishes after its batch was replaced can therefore never overwrite
        the conclusion of the batch now stored for the period.
        """
        status = "ok" if not conflicts else "conflict"
        verified_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect(write=True) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO verifications(batch_id, period, status, "
                "conflicts, verified_at) VALUES(?, ?, ?, ?, ?)",
                (batch_id, period, status, conflicts_to_json(conflicts), verified_at),
            )
        return VerificationRecord(
            batch_id=batch_id,
            period=period,
            status=status,
            conflicts=tuple(conflicts),
            verified_at=verified_at,
        )

    def batch_info(self, period: str) -> BatchInfo:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT b.created_at, "
                "       COUNT(c.cell) AS n, "
                "       COALESCE(SUM(c.population), 0) AS total "
                "FROM batches b LEFT JOIN cells c ON c.period = b.period "
                "WHERE b.period = ? GROUP BY b.period",
                (period,),
            ).fetchone()
        if row is None:
            raise UnknownPeriod(period)
        return BatchInfo(
            period=period,
            cell_count=row["n"],
            total_population=row["total"],
            created_at=row["created_at"],
        )

    def exists(self, period: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM batches WHERE period = ?", (period,)
            ).fetchone()
        return row is not None

    def catalog(self, period: str) -> List[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM batches WHERE period = ?", (period,)
            ).fetchone()
            if row is None:
                raise UnknownPeriod(period)
            rows = conn.execute(
                "SELECT cell FROM cells WHERE period = ? ORDER BY length(cell), cell",
                (period,),
            ).fetchall()
        return [r["cell"] for r in rows]

    def populations(self, period: str, cells: Iterable[str]) -> Dict[str, int]:
        """Read populations for exactly the requested cells of one batch."""
        wanted = list(dict.fromkeys(cells))
        if not wanted:
            return {}
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM batches WHERE period = ?", (period,)
            ).fetchone()
            if row is None:
                raise UnknownPeriod(period)
            found = _fetch_populations(conn, period, wanted)
        _require_all(period, wanted, found)
        return found

    @contextmanager
    def snapshot(self, period: str) -> Iterator["BatchSnapshot"]:
        """Consistent read view of one batch.

        The verification conclusion, the catalog and the populations fetched
        through the returned snapshot always come from the same transaction
        and therefore from the same batch, even if a writer replaces the
        batch concurrently.
        """
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.executescript(_SCHEMA)
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT batch_id FROM batches WHERE period = ?", (period,)
            ).fetchone()
            if row is None:
                raise UnknownPeriod(period)
            yield BatchSnapshot(conn, period, row["batch_id"])
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()


class BatchSnapshot:
    """Read-only consistent view of one period used by the solver."""

    def __init__(self, conn: sqlite3.Connection, period: str, batch_id: str) -> None:
        self._conn = conn
        self.period = period
        self.batch_id = batch_id

    def catalog(self) -> List[str]:
        rows = self._conn.execute(
            "SELECT cell FROM cells WHERE period = ? ORDER BY length(cell), cell",
            (self.period,),
        ).fetchall()
        return [r["cell"] for r in rows]

    def populations(self, cells: Iterable[str]) -> Dict[str, int]:
        wanted = list(dict.fromkeys(cells))
        if not wanted:
            return {}
        found = _fetch_populations(self._conn, self.period, wanted)
        _require_all(self.period, wanted, found)
        return found

    def all_populations(self) -> Dict[str, int]:
        """Every stored cell and headcount of this batch (for verification)."""
        rows = self._conn.execute(
            "SELECT cell, population FROM cells WHERE period = ?", (self.period,)
        ).fetchall()
        return {row["cell"]: row["population"] for row in rows}

    def verification(self) -> Optional[VerificationRecord]:
        """The saved conclusion for this batch, or ``None`` if never verified."""
        row = self._conn.execute(
            "SELECT status, conflicts, verified_at FROM verifications "
            "WHERE batch_id = ?",
            (self.batch_id,),
        ).fetchone()
        if row is None:
            return None
        return VerificationRecord(
            batch_id=self.batch_id,
            period=self.period,
            status=row["status"],
            conflicts=conflicts_from_json(row["conflicts"]),
            verified_at=row["verified_at"],
        )


def _fetch_populations(
    conn: sqlite3.Connection, period: str, wanted: Sequence[str]
) -> Dict[str, int]:
    found: Dict[str, int] = {}
    # Stay under SQLite's variable limit for large expressions.
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT cell, population FROM cells "
            f"WHERE period = ? AND cell IN ({placeholders})",
            (period, *chunk),
        ).fetchall()
        found.update({r["cell"]: r["population"] for r in rows})
    return found


def _require_all(period: str, wanted: Sequence[str], found: Dict[str, int]) -> None:
    missing = [cell for cell in wanted if cell not in found]
    if missing:
        raise MissingCells(period, missing)
