import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.archive import Archive
from app.main import app, get_archive


@pytest.fixture()
def archive(tmp_path):
    return Archive(tmp_path / "archive.sqlite3")


@pytest.fixture()
def client(archive):
    app.dependency_overrides[get_archive] = lambda: archive
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def submit(client, period, cells, replace=False):
    return client.post(
        "/archive",
        json={
            "period": period,
            "replace": replace,
            "cells": [{"cell": c, "population": p} for c, p in cells],
        },
    )


def query(client, period, cells):
    return client.post("/census/query", json={"period": period, "cells": cells})


def legacy_batch(archive, period, cells, batch_id):
    """Store a batch the way the pre-fix service did: no verification row."""
    conn = sqlite3.connect(archive.path)
    try:
        conn.execute(
            "INSERT INTO batches(period, batch_id, created_at) VALUES(?, ?, ?)",
            (period, batch_id, "2020-01-01T00:00:00+00:00"),
        )
        conn.executemany(
            "INSERT INTO cells(period, cell, population) VALUES(?, ?, ?)",
            [(period, cell, pop) for cell, pop in cells],
        )
        conn.commit()
    finally:
        conn.close()


def verification_rows(archive, period):
    conn = sqlite3.connect(archive.path)
    try:
        return conn.execute(
            "SELECT batch_id, status, conflicts FROM verifications "
            "WHERE period = ? ORDER BY rowid",
            (period,),
        ).fetchall()
    finally:
        conn.close()


def test_descendant_total_must_not_exceed_ancestor(client):
    # The reported bug: "0" records 10, "01" records 12, and querying
    # 00/02/03 used to answer -2.  The batch must now be rejected.
    r = submit(client, "p", [("0", 10), ("01", 12)])
    assert r.status_code == 422
    body = r.json()
    assert body["period"] == "p"
    assert body["conflicts"] == [
        {
            "ancestor": "0",
            "ancestor_population": 10,
            "cells": ["01"],
            "descendant_total": 12,
            "kind": "exceeds",
        }
    ]
    assert client.get("/archive/p").status_code == 404


def test_tiled_descendants_must_equal_ancestor(client):
    r = submit(client, "p", [("0", 10), ("00", 2), ("01", 2), ("02", 2), ("03", 2)])
    assert r.status_code == 422
    conflict = r.json()["conflicts"][0]
    assert conflict["ancestor"] == "0"
    assert conflict["kind"] == "mismatch"
    assert conflict["descendant_total"] == 8

    r = submit(client, "p", [("0", 10), ("00", 2), ("01", 2), ("02", 2), ("03", 4)])
    assert r.status_code == 201, r.text


def test_cells_covered_by_another_descendant_are_skipped(client):
    # "010" is already covered by "01" when "0" is checked: only "01" counts.
    r = submit(client, "p", [("0", 10), ("01", 6), ("010", 6)])
    assert r.status_code == 201, r.text
    # "010" is still checked against its own ancestor "01".
    r = submit(client, "q", [("0", 10), ("01", 6), ("010", 7)])
    assert r.status_code == 422
    conflict = r.json()["conflicts"][0]
    assert conflict["ancestor"] == "01"
    assert conflict["cells"] == ["010"]


def test_rejected_replace_keeps_batch_and_conclusion(client, archive):
    submit(client, "p", [("0", 10), ("01", 4)])
    rows_before = verification_rows(archive, "p")
    assert len(rows_before) == 1 and rows_before[0][1] == "ok"

    r = submit(client, "p", [("0", 10), ("01", 12)], replace=True)
    assert r.status_code == 422
    # The original batch and its verification conclusion are unchanged.
    assert client.get("/archive/p").json()["total_population"] == 14
    assert query(client, "p", ["00", "02", "03"]).json()["total_population"] == 6
    assert verification_rows(archive, "p") == rows_before


def test_legacy_batch_is_verified_on_first_query(client, archive):
    legacy_batch(archive, "old", [("0", 10), ("1", 20)], batch_id="legacy-ok")
    assert verification_rows(archive, "old") == []

    r = query(client, "old", ["0"])
    assert r.status_code == 200, r.text
    assert r.json()["total_population"] == 10
    # The conclusion is saved per batch and reused on later reads.
    rows = verification_rows(archive, "old")
    assert [(row[0], row[1]) for row in rows] == [("legacy-ok", "ok")]
    assert query(client, "old", ["0"]).status_code == 200


def test_legacy_bad_batch_only_reports_conflict_until_replaced(client, archive):
    legacy_batch(archive, "bad", [("0", 10), ("01", 12)], batch_id="legacy-bad")

    r = query(client, "bad", ["00", "02", "03"])
    assert r.status_code == 409
    body = r.json()
    assert body["period"] == "bad"
    assert body["conflicts"][0]["ancestor"] == "0"
    assert body["conflicts"][0]["cells"] == ["01"]
    # The conclusion is saved: the next read reports the same conflict.
    assert query(client, "bad", ["0"]).status_code == 409
    rows = verification_rows(archive, "bad")
    assert [(row[0], row[1]) for row in rows] == [("legacy-bad", "conflict")]

    # Replacing with a consistent batch resumes queries.
    r = submit(client, "bad", [("0", 10), ("01", 4)], replace=True)
    assert r.status_code == 201, r.text
    r = query(client, "bad", ["00", "02", "03"])
    assert r.status_code == 200, r.text
    assert r.json()["total_population"] == 6


def test_replace_is_allowed_during_legacy_verification(client, archive, monkeypatch):
    legacy_batch(archive, "p", [("0", 10), ("01", 12)], batch_id="legacy-1")

    import app.main as main_module

    real_find_conflicts = main_module.find_conflicts
    calls = {"n": 0}

    def verify_then_replace(populations):
        calls["n"] += 1
        if calls["n"] == 1:
            # A concurrent replacement while the old batch is being verified.
            archive.replace_batch("p", [("0", 10), ("01", 4)], allow_replace=True)
        return real_find_conflicts(populations)

    monkeypatch.setattr(main_module, "find_conflicts", verify_then_replace)

    # The fetch keeps using the batch it started with: the old, conflicting
    # one, so it reports the old batch's conflict.
    r = query(client, "p", ["00", "02", "03"])
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["ancestor"] == "0"

    # The late verification was saved under the old batch id and did not
    # overwrite the new batch's conclusion: queries resume on the new batch.
    rows = verification_rows(archive, "p")
    assert len(rows) == 2
    status_by_id = {row[0]: row[1] for row in rows}
    assert status_by_id["legacy-1"] == "conflict"
    r = query(client, "p", ["00", "02", "03"])
    assert r.status_code == 200, r.text
    assert r.json()["total_population"] == 6


def test_one_fetch_uses_one_batch_even_if_replaced_midway(
    client, archive, monkeypatch
):
    legacy_batch(archive, "p", [("0", 10), ("1", 20)], batch_id="legacy-2")

    import app.main as main_module

    real_find_conflicts = main_module.find_conflicts
    calls = {"n": 0}

    def verify_then_replace(populations):
        calls["n"] += 1
        if calls["n"] == 1:
            archive.replace_batch("p", [("0", 999), ("1", 999)], allow_replace=True)
        return real_find_conflicts(populations)

    monkeypatch.setattr(main_module, "find_conflicts", verify_then_replace)
    # Verification, catalog and populations all come from the old batch.
    r = query(client, "p", ["0"])
    assert r.status_code == 200, r.text
    assert r.json()["total_population"] == 10
    # The next fetch sees the new batch.
    assert query(client, "p", ["0"]).json()["total_population"] == 999


def test_same_second_replacements_are_distinct_batches(client, archive, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr("app.archive.datetime", FixedDatetime)
    submit(client, "p", [("0", 1), ("1", 2)])
    submit(client, "p", [("0", 3), ("1", 4)], replace=True)

    # Both replacements happened in the same second, yet each batch keeps its
    # own id and its own saved conclusion.
    rows = verification_rows(archive, "p")
    assert len(rows) == 2
    assert len({row[0] for row in rows}) == 2
    assert all(row[1] == "ok" for row in rows)
    assert query(client, "p", ["0"]).json()["total_population"] == 3
