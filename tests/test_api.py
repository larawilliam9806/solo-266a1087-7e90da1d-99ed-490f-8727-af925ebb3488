import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.archive import Archive, BatchConflict, InvalidBatch, MissingCells
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


def test_submit_and_read_metadata(client):
    r = submit(client, "202401", [("0", 10), ("1", 20), ("2", 30), ("3", 40)])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["cell_count"] == 4
    assert body["total_population"] == 100

    meta = client.get("/archive/202401")
    assert meta.status_code == 200
    assert meta.json()["total_population"] == 100


def test_root_cell_is_storable_and_queryable(client):
    r = submit(client, "root", [("", 1234)])
    assert r.status_code == 201, r.text
    assert r.json()["cell_count"] == 1
    q = query(client, "root", [""])
    assert q.status_code == 200, q.text
    assert q.json()["total_population"] == 1234
    assert q.json()["expression"] == 'pop("")'


def test_union_population_with_overlapping_cells(client):
    submit(
        client,
        "202402",
        [("0", 100), ("01", 40), ("02", 25), ("03", 20), ("00", 15)],
    )
    # "0" covers everything below it; adding "01" must not double-count.
    r = query(client, "202402", ["0", "01"])
    assert r.status_code == 200, r.text
    assert r.json()["total_population"] == 100
    assert r.json()["retrieved_cells"] <= 2


def test_partial_coverage_uses_subtraction(client):
    # Only "0" and "01" are archived; asking for 00/02/03 must be exact.
    submit(client, "202403", [("0", 100), ("01", 40)])
    r = query(client, "202403", ["00", "02", "03"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_population"] == 60
    assert body["retrieved_cells"] == 2
    assert any(t["coefficient"] == -1 for t in body["terms"])


def test_expression_reads_only_expression_cells(client, archive):
    # The catalog holds many cells, but a one-cell query must read exactly one.
    submit(
        client,
        "reads",
        [("0", 10), ("1", 20), ("2", 30), ("3", 40), ("01", 4), ("02", 5)],
    )
    body = query(client, "reads", ["2"]).json()
    assert body["expression"] == 'pop("2")'
    assert body["retrieved_cells"] == 1
    assert body["total_population"] == 30
    assert body["terms"] == [{"cell": "2", "coefficient": 1}]


def test_failed_replacement_keeps_original_batch(client, archive):
    submit(client, "202404", [("0", 1), ("1", 2), ("2", 3), ("3", 4)])
    # Rejecting without replace keeps the old batch.
    r = submit(client, "202404", [("0", 99), ("1", 99), ("2", 99), ("3", 99)])
    assert r.status_code == 409
    assert client.get("/archive/202404").json()["total_population"] == 10

    # An invalid replacement (duplicate cell) also keeps the old batch.
    bad = client.post(
        "/archive",
        json={
            "period": "202404",
            "replace": True,
            "cells": [
                {"cell": "0", "population": 5},
                {"cell": "0", "population": 6},
            ],
        },
    )
    assert bad.status_code == 422
    assert client.get("/archive/202404").json()["total_population"] == 10


def test_atomic_replace_changes_cells_and_counts(client):
    submit(client, "202405", [("0", 1), ("1", 2), ("2", 3), ("3", 4)])
    r = submit(client, "202405", [("00", 7), ("01", 8)], replace=True)
    assert r.status_code == 201, r.text
    assert r.json()["total_population"] == 15
    assert query(client, "202405", ["00", "01"]).json()["total_population"] == 15
    # Old cells are gone; reading the removed cells now reports missing data.
    assert client.get("/archive/202405").json()["cell_count"] == 2


def test_unknown_period_and_missing_archive(client):
    assert client.get("/archive/none").status_code == 404
    assert query(client, "none", ["0"]).status_code == 404

    submit(client, "202406", [("0", 5), ("1", 5)])
    r = query(client, "202406", ["2"])
    assert r.status_code == 422
    body = r.json()
    assert "missing_blocks" in body
    assert "2" in body["missing_blocks"]


def test_missing_archive_when_region_only_partially_covered(client):
    submit(client, "part", [("0", 5), ("00", 2), ("01", 3)])
    body = query(client, "part", ["0"]).json()
    assert body["expression"] == 'pop("0")'
    # Ask for a region that only exists as an unarchived slice of "0".
    r = query(client, "part", ["02"])
    assert r.status_code == 422
    assert "02" in r.json()["missing_blocks"]


def test_missing_cells_raised_at_archive_layer(archive):
    archive.replace_batch("p", [("0", 1)])
    with pytest.raises(MissingCells):
        archive.populations("p", ["0", "9"])
    with pytest.raises(MissingCells):
        archive.populations("p", ["1"])


def test_validation_errors(client):
    assert submit(client, "x", [("4", 1)]).status_code == 422  # bad digit
    assert submit(client, "x", [("0000000000000", 1)]).status_code == 422  # 13 digits
    assert client.post("/archive", json={"period": "", "cells": []}).status_code == 422
    assert submit(client, "x", [("0", -1)]).status_code == 422
    assert query(client, "x", ["9"]).status_code == 422


def test_empty_query_returns_zero(client):
    submit(client, "202407", [("0", 5)])
    r = query(client, "202407", [])
    assert r.status_code == 200
    assert r.json()["total_population"] == 0
    assert r.json()["retrieved_cells"] == 0
    assert r.json()["expression"] == "0"


def test_deep_cells_maximum_depth(client):
    deep = "012301230123"
    r = submit(client, "deep", [(deep, 77)])
    assert r.status_code == 201, r.text
    q = query(client, "deep", [deep])
    assert q.status_code == 200, q.text
    assert q.json()["total_population"] == 77


def test_archive_class_rejects_duplicate_batch(archive):
    archive.replace_batch("p", [("0", 1)])
    with pytest.raises(BatchConflict):
        archive.replace_batch("p", [("0", 2)])
    archive.replace_batch("p", [("0", 2)], allow_replace=True)
    assert archive.batch_info("p").total_population == 2


def test_archive_class_duplicate_cells(archive):
    with pytest.raises(InvalidBatch):
        archive.replace_batch("p", [("0", 1), ("0", 2)])


def test_catalog_and_population_come_from_same_batch(archive, monkeypatch):
    """A replacement between solve and read must not mix two batches."""
    archive.replace_batch("p", [("0", 10), ("1", 20)])

    import app.main as main_module

    real_analyze = main_module.analyze
    calls = {"n": 0}

    def analyze_then_replace(*args, **kwargs):
        calls["n"] += 1
        analysis = real_analyze(*args, **kwargs)
        if calls["n"] == 1:
            # Concurrent replacement in a *different* connection.
            archive.replace_batch("p", [("0", 999), ("1", 999)], allow_replace=True)
        return analysis

    monkeypatch.setattr(main_module, "analyze", analyze_then_replace)
    app.dependency_overrides[get_archive] = lambda: archive
    try:
        with TestClient(app) as c:
            body = c.post(
                "/census/query", json={"period": "p", "cells": ["0"]}
            ).json()
    finally:
        app.dependency_overrides.clear()
    assert body["total_population"] == 10
