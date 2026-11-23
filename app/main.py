"""FastAPI service exposing the census archive.

Endpoints
---------
``POST /archive``            submit (or atomically replace) one period's batch
``GET  /archive/{period}``   batch metadata
``POST /census/query``       minimal-read expression + union population

The census endpoint works inside one SQLite read snapshot: it first checks
the batch's saved consistency conclusion (verifying legacy batches lazily on
first read), then derives the expression from the period's catalog only, and
finally reads headcounts for exactly the cells that appear in the expression.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from .archive import (
    Archive,
    BatchConflict,
    InconsistentBatch,
    InvalidBatch,
    MissingCells,
    UnknownPeriod,
    VerificationRecord,
    validate_cell,
)
from .solver import MAX_DEPTH, analyze
from .verify import conflict_to_dict, find_conflicts

MAX_CELLS_PER_REQUEST = 100_000
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "archive.sqlite3"


def default_database_path() -> str:
    return os.environ.get("CENSUS_DB", str(DEFAULT_DB))


def get_archive() -> Archive:
    return Archive(default_database_path())


def _check_cell(cell: str) -> str:
    try:
        return validate_cell(cell)
    except InvalidBatch as exc:
        # Re-raise as ValueError so pydantic reports it as a validation error.
        raise ValueError(str(exc)) from exc


class CellEntry(BaseModel):
    cell: str = Field(..., description='quadtree cell id; the root is ""')
    population: int = Field(..., ge=0)


class BatchSubmission(BaseModel):
    period: str = Field(..., min_length=1, description="statistical period label")
    replace: bool = Field(
        False,
        description="replace an existing batch atomically when true",
    )
    cells: List[CellEntry] = Field(
        default_factory=list, max_length=MAX_CELLS_PER_REQUEST
    )

    @field_validator("period")
    @classmethod
    def _period_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("period must not be blank")
        return value

    @field_validator("cells")
    @classmethod
    def _cells_valid(cls, value: List[CellEntry]) -> List[CellEntry]:
        seen = set()
        for entry in value:
            _check_cell(entry.cell)
            if entry.cell in seen:
                raise ValueError(f"duplicate cell id {entry.cell!r} in batch")
            seen.add(entry.cell)
        return value


class BatchResponse(BaseModel):
    period: str
    cell_count: int
    total_population: int
    created_at: str
    replaced: bool


class QueryRequest(BaseModel):
    period: str = Field(..., min_length=1)
    cells: List[str] = Field(..., max_length=MAX_CELLS_PER_REQUEST)

    @field_validator("cells")
    @classmethod
    def _cells_valid(cls, value: List[str]) -> List[str]:
        return [_check_cell(cell) for cell in value]


class ExpressionTermModel(BaseModel):
    cell: str
    coefficient: int


class CensusResponse(BaseModel):
    period: str
    cells: List[str]
    expression: str
    terms: List[ExpressionTermModel]
    retrieved_cells: int
    total_population: int


class MissingArchiveResponse(BaseModel):
    detail: str
    period: str
    missing_blocks: List[str]
    truncated: bool = False


class ConflictModel(BaseModel):
    ancestor: str
    ancestor_population: int
    cells: List[str]
    descendant_total: int
    kind: str


class BatchConflictsResponse(BaseModel):
    detail: str
    period: str
    conflicts: List[ConflictModel]


app = FastAPI(
    title="Census Archive Service",
    description=(
        "Quadtree census archive. Submit whole statistical-period batches "
        "atomically and compute union populations with minimal-read expressions."
    ),
    version="1.0.0",
)


@app.exception_handler(UnknownPeriod)
async def _unknown_period_handler(request: Request, exc: UnknownPeriod) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"detail": str(exc), "period": exc.period},
    )


@app.exception_handler(MissingCells)
async def _missing_cells_handler(request: Request, exc: MissingCells) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(exc),
            "period": exc.period,
            "missing_blocks": list(exc.cells),
            "truncated": False,
        },
    )


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/archive", response_model=BatchResponse, status_code=201)
def submit_batch(
    submission: BatchSubmission,
    archive: Archive = Depends(get_archive),
) -> BatchResponse:
    try:
        info = archive.replace_batch(
            submission.period,
            [(entry.cell, entry.population) for entry in submission.cells],
            allow_replace=submission.replace,
        )
    except BatchConflict as exc:
        raise HTTPException(
            status_code=409,
            detail=f"{exc}; pass replace=true to replace it atomically",
        ) from exc
    except InconsistentBatch as exc:
        # The submission contradicts itself; the stored batch and its saved
        # verification conclusion are left untouched.
        return JSONResponse(
            status_code=422,
            content=BatchConflictsResponse(
                detail=str(exc),
                period=exc.period,
                conflicts=[
                    ConflictModel(**conflict_to_dict(conflict))
                    for conflict in exc.conflicts
                ],
            ).model_dump(),
        )
    except InvalidBatch as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return BatchResponse(
        period=info.period,
        cell_count=info.cell_count,
        total_population=info.total_population,
        created_at=info.created_at,
        replaced=submission.replace,
    )


@app.get("/archive/{period}", response_model=BatchResponse)
def read_batch(period: str, archive: Archive = Depends(get_archive)) -> BatchResponse:
    info = archive.batch_info(period)
    return BatchResponse(
        period=info.period,
        cell_count=info.cell_count,
        total_population=info.total_population,
        created_at=info.created_at,
        replaced=False,
    )


@app.post("/census/query", response_model=CensusResponse)
def query_census(
    request: QueryRequest,
    archive: Archive = Depends(get_archive),
) -> CensusResponse:
    with archive.snapshot(request.period) as snapshot:
        record = snapshot.verification()
        if record is None:
            # A batch stored before consistency checks existed: verify it now
            # from this snapshot and save the conclusion under its batch id.
            # The read snapshot does not block a concurrent replacement, and
            # the conclusion is keyed by batch id, so this late verification
            # can never overwrite a newer batch's conclusion.
            record = archive.record_verification(
                snapshot.batch_id,
                request.period,
                find_conflicts(snapshot.all_populations()),
            )
        if record.status == "conflict":
            # A bad archive only reports its conflicts until a consistent
            # batch replaces it.
            return _conflict_response(request.period, record)

        # Phase 1: derive the minimal expression from the catalog alone.  No
        # headcount is read and no finest cell is enumerated.
        try:
            analysis = analyze(snapshot.catalog(), request.cells, depth=MAX_DEPTH)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not analysis.feasible:
            return _missing_response(request.period, analysis)
        solution = analysis.solution

        # Phase 2: read only the cells that occur in the expression.
        populations = snapshot.populations(term.cell for term in solution.terms)
        total = solution.evaluate(populations.__getitem__)

    return CensusResponse(
        period=request.period,
        cells=list(dict.fromkeys(request.cells)),
        expression=solution.render(),
        terms=[
            ExpressionTermModel(cell=term.cell, coefficient=term.coefficient)
            for term in solution.terms
        ],
        retrieved_cells=solution.retrieved_cells,
        total_population=total,
    )


def _conflict_response(period: str, record: VerificationRecord) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content=BatchConflictsResponse(
            detail=(
                "the archived batch for this period failed consistency "
                "verification; replace it with a consistent batch to resume "
                "queries"
            ),
            period=period,
            conflicts=[
                ConflictModel(**conflict_to_dict(conflict))
                for conflict in record.conflicts
            ],
        ).model_dump(),
    )


def _missing_response(period: str, analysis) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=MissingArchiveResponse(
            detail=(
                "the archive for this period cannot support an exact "
                "expression for the requested cells; archive the listed "
                "missing blocks and retry"
            ),
            period=period,
            missing_blocks=list(analysis.missing_blocks),
            truncated=analysis.missing_truncated,
        ).model_dump(),
    )
