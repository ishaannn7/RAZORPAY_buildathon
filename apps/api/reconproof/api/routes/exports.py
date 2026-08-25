"""Report exports.

Reconciliation output is opened in a spreadsheet by the people who use it, so
every cell is neutralized against formula injection on the way out. A value
beginning with ``=``, ``+``, ``@`` or a tab would otherwise execute when the file
is opened.

Amounts are written in major units as an exact decimal string derived from the
stored integer, never from a float.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from reconproof.audit.log import record as audit_record
from reconproof.db.models import (
    AuditEvent,
    ReconciliationBatch,
    ReconciliationException,
    ReconciliationMatch,
    SourceRecord,
)
from reconproof.db.session import get_db
from reconproof.domain.entities import Actor, AuditAction
from reconproof.domain.money import Money
from reconproof.ingest.parsers import looks_like_formula_injection

router = APIRouter(prefix="/batches", tags=["exports"])


def _safe(value: Any) -> str:
    """Render a cell so it cannot execute in a spreadsheet."""
    if value is None:
        return ""
    text = str(value)
    if looks_like_formula_injection(text):
        # A leading apostrophe forces text interpretation in Excel and Sheets.
        return f"'{text}"
    return text


ExportFormat = Literal["csv", "json"]


def _csv_response(
    rows: list[dict[str, Any]], columns: list[str], filename: str
) -> StreamingResponse:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _safe(row.get(column)) for column in columns})
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _json_response(rows: list[dict[str, Any]], columns: list[str], filename: str) -> Response:
    """The same rows as the CSV export, as a JSON array.

    No formula-injection neutralization here — that guard exists because a
    leading ``=``/``+``/``@`` executes when a *spreadsheet* opens the cell; a
    JSON consumer never interprets a string that way, so applying `_safe`
    would just corrupt values (e.g. prefixing a genuine reference like
    ``=REFUND-1`` with a stray apostrophe) for no protective benefit.
    """
    payload = [{column: row.get(column) for column in columns} for row in rows]
    return Response(
        content=json.dumps(payload, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _export_response(
    rows: list[dict[str, Any]], columns: list[str], stem: str, export_format: ExportFormat
) -> Response:
    if export_format == "json":
        return _json_response(rows, columns, f"{stem}.json")
    return _csv_response(rows, columns, f"{stem}.csv")


def _get_batch(db: Session, batch_id: str) -> ReconciliationBatch:
    batch = db.get(ReconciliationBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"batch {batch_id} not found")
    return batch


@router.get("/{batch_id}/export/exceptions")
def export_exceptions(
    batch_id: str,
    db: Annotated[Session, Depends(get_db)],
    format: Annotated[ExportFormat, Query()] = "csv",
) -> Response:
    """The exception report: everything left unexplained, with its amount."""
    batch = _get_batch(db, batch_id)
    items = list(
        db.execute(
            select(ReconciliationException)
            .where(ReconciliationException.batch_id == batch_id)
            .order_by(ReconciliationException.amount_subunits.desc())
        ).scalars()
    )
    record_ids = {item.subject_record_id for item in items}
    records = {
        record.id: record
        for record in db.execute(
            select(SourceRecord).where(SourceRecord.id.in_(record_ids or [""]))
        ).scalars()
    }

    rows = []
    for item in items:
        subject = records.get(item.subject_record_id)
        rows.append(
            {
                "exception_id": item.id,
                "category": item.category.value,
                "status": item.status.value,
                "amount": Money(item.amount_subunits, item.currency).format(),
                "currency": item.currency,
                "subject_kind": subject.record_kind.value if subject else "",
                "subject_reference": (
                    subject.settlement_ref
                    or subject.payment_ref
                    or subject.order_ref
                    or subject.bank_ref
                    or subject.external_id
                    if subject
                    else ""
                ),
                "subject_source": subject.source_kind.value if subject else "",
                "occurred_at": (
                    subject.occurred_at.isoformat() if subject and subject.occurred_at else ""
                ),
                "best_score": item.best_score,
                "best_risk": item.best_risk,
                "blocking_checks": "; ".join(item.blocking_invariants or []),
                "summary": item.summary,
                "explanation": item.explanation,
            }
        )

    audit_record(
        db,
        action=AuditAction.EXPORT_GENERATED,
        actor=Actor.HUMAN,
        batch_id=batch.id,
        detail={"report": "exceptions", "rows": len(rows), "format": format},
        message=f"Exception report exported ({len(rows)} rows, {format})",
    )
    db.commit()

    return _export_response(
        rows,
        [
            "exception_id",
            "category",
            "status",
            "amount",
            "currency",
            "subject_kind",
            "subject_reference",
            "subject_source",
            "occurred_at",
            "best_score",
            "best_risk",
            "blocking_checks",
            "summary",
            "explanation",
        ],
        f"reconproof-exceptions-{batch.name}",
        format,
    )


@router.get("/{batch_id}/export/matches")
def export_matches(
    batch_id: str,
    db: Annotated[Session, Depends(get_db)],
    decision: Annotated[str | None, Query()] = None,
    format: Annotated[ExportFormat, Query()] = "csv",
) -> Response:
    """The reconciled ledger, with how each link was decided."""
    batch = _get_batch(db, batch_id)
    statement = select(ReconciliationMatch).where(ReconciliationMatch.batch_id == batch_id)
    if decision:
        statement = statement.where(ReconciliationMatch.decision == decision)
    matches = list(db.execute(statement).scalars())

    record_ids: set[str] = set()
    for match in matches:
        record_ids.add(match.left_record_id)
        record_ids.add(match.right_record_id)
    records = {
        record.id: record
        for record in db.execute(
            select(SourceRecord).where(SourceRecord.id.in_(record_ids or [""]))
        ).scalars()
    }

    def reference(record: SourceRecord | None) -> str:
        if record is None:
            return ""
        return (
            record.settlement_ref
            or record.payment_ref
            or record.order_ref
            or record.bank_ref
            or record.external_id
            or ""
        )

    rows = [
        {
            "match_id": match.id,
            "relation": match.relation.value,
            "decision": match.decision.value,
            "method": match.method.value,
            "allocated": Money(match.allocated_subunits, match.currency).format(),
            "currency": match.currency,
            "score": match.score,
            "risk": match.risk,
            "from_kind": (
                records[match.left_record_id].record_kind.value
                if match.left_record_id in records
                else ""
            ),
            "from_reference": reference(records.get(match.left_record_id)),
            "to_kind": (
                records[match.right_record_id].record_kind.value
                if match.right_record_id in records
                else ""
            ),
            "to_reference": reference(records.get(match.right_record_id)),
            "checks_passed": "; ".join(match.invariants_passed or []),
            "checks_failed": "; ".join(name for name in (match.invariants_failed or []) if name),
            "rationale": match.rationale,
        }
        for match in matches
    ]

    audit_record(
        db,
        action=AuditAction.EXPORT_GENERATED,
        actor=Actor.HUMAN,
        batch_id=batch.id,
        detail={"report": "matches", "rows": len(rows), "decision": decision, "format": format},
        message=f"Match report exported ({len(rows)} rows, {format})",
    )
    db.commit()

    return _export_response(
        rows,
        [
            "match_id",
            "relation",
            "decision",
            "method",
            "allocated",
            "currency",
            "score",
            "risk",
            "from_kind",
            "from_reference",
            "to_kind",
            "to_reference",
            "checks_passed",
            "checks_failed",
            "rationale",
        ],
        f"reconproof-matches-{batch.name}",
        format,
    )


@router.get("/{batch_id}/export/audit")
def export_audit(
    batch_id: str,
    db: Annotated[Session, Depends(get_db)],
    format: Annotated[ExportFormat, Query()] = "csv",
) -> Response:
    """The audit package: the full decision history for this batch."""
    batch = _get_batch(db, batch_id)
    events = list(
        db.execute(
            select(AuditEvent).where(AuditEvent.batch_id == batch_id).order_by(AuditEvent.sequence)
        ).scalars()
    )
    rows = [
        {
            "sequence": event.sequence,
            "recorded_at": event.created_at.isoformat(),
            "actor": event.actor.value,
            "actor_detail": event.actor_detail,
            "action": event.action.value,
            "subject_type": event.subject_type,
            "subject_id": event.subject_id,
            "input_sha256": event.input_sha256,
            "model_version_id": event.model_version_id,
            "policy_version_id": event.policy_version_id,
            "message": event.message,
        }
        for event in events
    ]
    return _export_response(
        rows,
        [
            "sequence",
            "recorded_at",
            "actor",
            "actor_detail",
            "action",
            "subject_type",
            "subject_id",
            "input_sha256",
            "model_version_id",
            "policy_version_id",
            "message",
        ],
        f"reconproof-audit-{batch.name}",
        format,
    )
