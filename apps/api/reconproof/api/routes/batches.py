"""Batch, record, match and audit endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reconproof.api.schemas import (
    Amount,
    AuditEntry,
    BatchDetail,
    BatchMetrics,
    BatchSummary,
    GraphResponse,
    MatchSummary,
    SourceFileSummary,
)
from reconproof.api.serializers import (
    serialize_audit,
    serialize_graph_edge,
    serialize_graph_node,
    serialize_match,
    serialize_record,
)
from reconproof.audit.log import record as audit_record
from reconproof.config import Settings, get_settings
from reconproof.db.models import (
    AuditEvent,
    ModelVersion,
    PolicyVersion,
    ReconciliationBatch,
    ReconciliationException,
    ReconciliationMatch,
    SourceFile,
    SourceRecord,
)
from reconproof.db.session import get_db
from reconproof.domain.entities import (
    Actor,
    AuditAction,
    BatchStatus,
    ExceptionStatus,
    MatchDecision,
    SourceKind,
)
from reconproof.ingest.loader import StructuralIngestError, ingest_file
from reconproof.ingest.schemas import detect_source_kind
from reconproof.pipeline import ReconciliationPipeline
from reconproof.runtime import load_active_scorer

router = APIRouter(prefix="/batches", tags=["batches"])

#: Cap on graph edges returned in one response. A 3,000-record batch has
#: thousands of edges, and shipping all of them would stall the browser rather
#: than inform anyone.
GRAPH_EDGE_LIMIT = 600


def _get_batch(db: Session, batch_id: str) -> ReconciliationBatch:
    batch = db.get(ReconciliationBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"batch {batch_id} not found")
    return batch


def _summarize(db: Session, batch: ReconciliationBatch) -> BatchSummary:
    record_count = db.execute(
        select(func.count()).select_from(SourceRecord).where(SourceRecord.batch_id == batch.id)
    ).scalar_one()
    exception_count = db.execute(
        select(func.count())
        .select_from(ReconciliationException)
        .where(ReconciliationException.batch_id == batch.id)
    ).scalar_one()
    open_count = db.execute(
        select(func.count())
        .select_from(ReconciliationException)
        .where(
            ReconciliationException.batch_id == batch.id,
            ReconciliationException.status == ExceptionStatus.OPEN,
        )
    ).scalar_one()
    unresolved = db.execute(
        select(func.coalesce(func.sum(ReconciliationException.amount_subunits), 0)).where(
            ReconciliationException.batch_id == batch.id,
            ReconciliationException.status.in_(
                [
                    ExceptionStatus.OPEN,
                    ExceptionStatus.INVESTIGATING,
                    ExceptionStatus.AWAITING_APPROVAL,
                ]
            ),
        )
    ).scalar_one()
    return BatchSummary(
        id=batch.id,
        name=batch.name,
        status=batch.status.value,
        currency=batch.currency,
        created_at=batch.created_at,
        started_at=batch.started_at,
        completed_at=batch.completed_at,
        record_count=record_count,
        exception_count=exception_count,
        open_exception_count=open_count,
        unresolved=Amount.of(unresolved, batch.currency),
        automation_restricted=batch.automation_restricted,
        restriction_reason=batch.restriction_reason,
        dataset_seed=batch.dataset_seed,
    )


def _latest_metrics(db: Session, batch: ReconciliationBatch) -> BatchMetrics | None:
    """Rebuild the metrics block from the run's audit entry.

    Read from the audit log rather than recomputed, so what the UI shows is
    exactly what the run recorded at the time it completed.
    """
    event = (
        db.execute(
            select(AuditEvent)
            .where(
                AuditEvent.batch_id == batch.id,
                AuditEvent.action == AuditAction.RUN_COMPLETED,
            )
            .order_by(AuditEvent.sequence.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if event is None or not event.detail:
        return None
    detail: dict[str, Any] = event.detail
    currency = batch.currency
    return BatchMetrics(
        total_records=detail.get("total_records", 0),
        records_by_kind=detail.get("records_by_kind", {}),
        exact_links=detail.get("exact_links", 0),
        candidates_generated=detail.get("candidates_generated", 0),
        auto_accepted=detail.get("auto_accepted", 0),
        sent_to_review=detail.get("sent_to_review", 0),
        rejected_by_invariant=detail.get("rejected_by_invariant", 0),
        displaced_by_assignment=detail.get("displaced_by_assignment", 0),
        forced_review_by_tie=detail.get("forced_review_by_tie", 0),
        balanced_settlements=detail.get("balanced_settlements", 0),
        unbalanced_settlements=detail.get("unbalanced_settlements", 0),
        automatic_match_rate=detail.get("automatic_match_rate", 0.0),
        money_weighted_rate=detail.get("money_weighted_rate", 0.0),
        settlement_value=Amount.of(detail.get("settlement_value_subunits", 0), currency),
        settlement_value_traced=Amount.of(
            detail.get("settlement_value_traced_subunits", 0), currency
        ),
        unexplained=Amount.of(detail.get("unexplained_subunits", 0), currency),
        exception_total=Amount.of(detail.get("exception_subunits", 0), currency),
        unresolved_value_fully_represented=detail.get("unresolved_value_fully_represented", False),
        exceptions_by_category=detail.get("exceptions_by_category", {}),
        scorer=detail.get("scorer", "heuristic"),
        accept_threshold=detail.get("accept_threshold"),
        risk_budget=detail.get("risk_budget"),
        duration_ms=detail.get("duration_ms", 0),
    )


@router.get("", response_model=list[BatchSummary])
def list_batches(
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[BatchSummary]:
    batches = list(
        db.execute(
            select(ReconciliationBatch).order_by(ReconciliationBatch.created_at.desc()).limit(limit)
        ).scalars()
    )
    return [_summarize(db, batch) for batch in batches]


@router.post("", response_model=BatchDetail, status_code=201)
def create_batch(
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Query(min_length=1, max_length=200)],
    currency: Annotated[str, Query(min_length=3, max_length=3)] = "INR",
) -> BatchDetail:
    batch = ReconciliationBatch(name=name, currency=currency.upper(), status=BatchStatus.DRAFT)
    db.add(batch)
    db.flush()
    audit_record(
        db,
        action=AuditAction.BATCH_CREATED,
        actor=Actor.HUMAN,
        batch_id=batch.id,
        subject_type="batch",
        subject_id=batch.id,
        message=f"Batch {name} created",
    )
    db.commit()
    return BatchDetail(**_summarize(db, batch).model_dump())


@router.get("/{batch_id}", response_model=BatchDetail)
def get_batch(batch_id: str, db: Annotated[Session, Depends(get_db)]) -> BatchDetail:
    batch = _get_batch(db, batch_id)
    sources = list(db.execute(select(SourceFile).where(SourceFile.batch_id == batch.id)).scalars())
    policy = db.get(PolicyVersion, batch.policy_version_id) if batch.policy_version_id else None
    model = db.get(ModelVersion, batch.model_version_id) if batch.model_version_id else None
    return BatchDetail(
        **_summarize(db, batch).model_dump(),
        sources=[
            SourceFileSummary(
                id=source.id,
                source_kind=source.source_kind.value,
                filename=source.filename,
                content_sha256=source.content_sha256,
                byte_size=source.byte_size,
                row_count=source.row_count,
                accepted_rows=source.accepted_rows,
                rejected_rows=source.rejected_rows,
                detected_columns=source.detected_columns or {},
                validation_errors=source.validation_errors or [],
            )
            for source in sources
        ],
        metrics=_latest_metrics(db, batch),
        policy_version=f"{policy.name}@{policy.version}" if policy else None,
        model_version=model.name if model else None,
    )


@router.post("/{batch_id}/sources", response_model=SourceFileSummary, status_code=201)
async def upload_source(
    batch_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    source_kind: Annotated[str | None, Query()] = None,
    file: UploadFile = File(...),
) -> SourceFileSummary:
    batch = _get_batch(db, batch_id)
    filename = file.filename or "upload.csv"
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in settings.allowed_upload_suffixes:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported file type {suffix!r}; allowed: "
            f"{', '.join(settings.allowed_upload_suffixes)}",
        )
    payload = await file.read()
    if len(payload) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="file too large")

    resolved_kind: SourceKind
    if source_kind:
        try:
            resolved_kind = SourceKind(source_kind)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"unknown source kind {source_kind!r}"
            ) from exc
    else:
        # A guess is only a convenience. It must not silently reinterpret a file,
        # so an unrecognized layout is an error rather than a default.
        header = payload.split(b"\n", 1)[0].decode("utf-8", errors="replace")
        detected, _score = detect_source_kind([cell.strip() for cell in header.split(",")])
        if detected is None:
            raise HTTPException(
                status_code=400,
                detail="could not determine the source kind; pass source_kind explicitly",
            )
        resolved_kind = detected

    try:
        result = ingest_file(
            db,
            batch=batch,
            source_kind=resolved_kind,
            filename=filename,
            payload=payload,
        )
    except StructuralIngestError as exc:
        # The transaction is discarded, so the batch is untouched.
        db.rollback()
        audit_record(
            db,
            action=AuditAction.SOURCE_REJECTED,
            actor=Actor.SYSTEM,
            batch_id=batch.id,
            detail={"errors": [error.to_dict() for error in exc.errors[:50]]},
            message=exc.message,
        )
        db.commit()
        raise HTTPException(status_code=422, detail=exc.message) from exc

    if batch.status is BatchStatus.DRAFT:
        batch.status = BatchStatus.READY
    db.commit()

    source = result.source_file
    assert source is not None
    return SourceFileSummary(
        id=source.id,
        source_kind=source.source_kind.value,
        filename=source.filename,
        content_sha256=source.content_sha256,
        byte_size=source.byte_size,
        row_count=source.row_count,
        accepted_rows=source.accepted_rows,
        rejected_rows=source.rejected_rows,
        detected_columns=source.detected_columns or {},
        validation_errors=source.validation_errors or [],
    )


@router.post("/{batch_id}/run", response_model=BatchDetail)
def run_batch(
    batch_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BatchDetail:
    batch = _get_batch(db, batch_id)
    if batch.status is BatchStatus.RUNNING:
        raise HTTPException(status_code=409, detail="batch is already running")
    record_count = db.execute(
        select(func.count()).select_from(SourceRecord).where(SourceRecord.batch_id == batch.id)
    ).scalar_one()
    if record_count == 0:
        raise HTTPException(status_code=400, detail="batch has no records to reconcile")

    # Re-running must not stack results on top of the previous attempt.
    _clear_previous_run(db, batch)

    scorer = load_active_scorer(settings)
    pipeline = ReconciliationPipeline(db, settings=settings, scorer=scorer)
    try:
        pipeline.run(batch)
    except Exception as exc:
        db.rollback()
        batch = _get_batch(db, batch_id)
        batch.status = BatchStatus.FAILED
        batch.failure_reason = str(exc)
        audit_record(
            db,
            action=AuditAction.RUN_FAILED,
            actor=Actor.PIPELINE,
            batch_id=batch.id,
            message=f"Reconciliation failed: {exc}",
        )
        db.commit()
        raise HTTPException(status_code=500, detail=f"reconciliation failed: {exc}") from exc
    db.commit()
    return get_batch(batch_id, db)


def _clear_previous_run(db: Session, batch: ReconciliationBatch) -> None:
    """Remove derived results from an earlier run.

    Source records, source files and the audit log are never touched: the audit
    trail of a superseded run is exactly the history an auditor needs.
    """
    from reconproof.db.models import (
        AccountingCheck,
        AgentRun,
        EvidenceItem,
        HumanResolution,
        MatchCandidate,
    )

    for model in (
        HumanResolution,
        EvidenceItem,
        AccountingCheck,
        ReconciliationMatch,
        MatchCandidate,
        AgentRun,
        ReconciliationException,
    ):
        for row in db.execute(select(model).where(model.batch_id == batch.id)).scalars():
            db.delete(row)
    db.flush()


@router.get("/{batch_id}/matches", response_model=list[MatchSummary])
def list_matches(
    batch_id: str,
    db: Annotated[Session, Depends(get_db)],
    decision: Annotated[str | None, Query()] = None,
    relation: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[MatchSummary]:
    _get_batch(db, batch_id)
    statement = select(ReconciliationMatch).where(ReconciliationMatch.batch_id == batch_id)
    if decision:
        try:
            statement = statement.where(ReconciliationMatch.decision == MatchDecision(decision))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"unknown decision {decision!r}") from exc
    if relation:
        statement = statement.where(ReconciliationMatch.relation == relation)
    statement = (
        statement.order_by(ReconciliationMatch.score.desc().nullslast()).limit(limit).offset(offset)
    )
    matches = list(db.execute(statement).scalars())
    records = _record_map(db, matches)
    return [
        serialize_match(match, records[match.left_record_id], records[match.right_record_id])
        for match in matches
        if match.left_record_id in records and match.right_record_id in records
    ]


def _record_map(db: Session, matches: list[ReconciliationMatch]) -> dict[str, SourceRecord]:
    ids = {match.left_record_id for match in matches} | {match.right_record_id for match in matches}
    if not ids:
        return {}
    return {
        record.id: record
        for record in db.execute(select(SourceRecord).where(SourceRecord.id.in_(ids))).scalars()
    }


@router.get("/{batch_id}/graph", response_model=GraphResponse)
def get_graph(
    batch_id: str,
    db: Annotated[Session, Depends(get_db)],
    decision: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=GRAPH_EDGE_LIMIT)] = 200,
) -> GraphResponse:
    _get_batch(db, batch_id)
    statement = select(ReconciliationMatch).where(ReconciliationMatch.batch_id == batch_id)
    if decision:
        statement = statement.where(ReconciliationMatch.decision == MatchDecision(decision))
    total = db.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
    matches = list(db.execute(statement.limit(limit)).scalars())
    records = _record_map(db, matches)
    return GraphResponse(
        nodes=[serialize_graph_node(record) for record in records.values()],
        edges=[
            serialize_graph_edge(match)
            for match in matches
            if match.left_record_id in records and match.right_record_id in records
        ],
        truncated=total > len(matches),
        total_edges=total,
    )


@router.get("/{batch_id}/records", response_model=list[dict[str, Any]])
def list_records(
    batch_id: str,
    db: Annotated[Session, Depends(get_db)],
    record_kind: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    _get_batch(db, batch_id)
    statement = select(SourceRecord).where(SourceRecord.batch_id == batch_id)
    if record_kind:
        statement = statement.where(SourceRecord.record_kind == record_kind)
    statement = (
        statement.order_by(SourceRecord.occurred_at.desc().nullslast()).limit(limit).offset(offset)
    )
    return [serialize_record(record).model_dump() for record in db.execute(statement).scalars()]


@router.get("/{batch_id}/audit", response_model=list[AuditEntry])
def list_audit(
    batch_id: str,
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditEntry]:
    _get_batch(db, batch_id)
    events = list(
        db.execute(
            select(AuditEvent)
            .where(AuditEvent.batch_id == batch_id)
            .order_by(AuditEvent.sequence.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    return [serialize_audit(event) for event in events]
