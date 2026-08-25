"""Razorpay integration: pull sync and push webhook receiver.

Both entry points are additive to the file-upload workflow, not a
replacement for it — the product is fully demonstrable without Razorpay
credentials, by construction (see `config.py`). These routes only exist when
a merchant wants to sync real Razorpay data instead of uploading an export.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from reconproof.api.schemas import (
    RazorpaySyncRequest,
    RazorpaySyncResult,
    SourceFileSummary,
    WebhookIngestResult,
)
from reconproof.audit.log import record as audit_record
from reconproof.config import Settings, get_settings
from reconproof.db.models import ReconciliationBatch, SourceFile
from reconproof.db.session import get_db
from reconproof.domain.entities import Actor, AuditAction, BatchStatus, SourceKind
from reconproof.ingest.loader import StructuralIngestError, ingest_file
from reconproof.integrations.razorpay import (
    RazorpayAPIError,
    RazorpayClient,
    RazorpayConfigError,
    payments_to_csv,
    refunds_to_csv,
    verify_webhook_signature,
    webhook_event_to_csv,
)

integrations_router = APIRouter(prefix="/integrations", tags=["integrations"])
webhooks_router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _get_batch(db: Session, batch_id: str) -> ReconciliationBatch:
    batch = db.get(ReconciliationBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"batch {batch_id} not found")
    return batch


def _source_summary(source: SourceFile) -> SourceFileSummary:
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


def _ingest_or_422(
    db: Session,
    *,
    batch: ReconciliationBatch,
    source_kind: SourceKind,
    filename: str,
    payload: bytes,
):
    try:
        return ingest_file(
            db, batch=batch, source_kind=source_kind, filename=filename, payload=payload
        )
    except StructuralIngestError as exc:
        # The transaction is discarded, so anything already ingested by this
        # same sync/webhook call is discarded with it — a sync is atomic, not
        # a best-effort partial import.
        db.rollback()
        batch = _get_batch(db, batch.id)
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


def _build_client(settings: Settings) -> RazorpayClient:
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        raise RazorpayConfigError(
            "Razorpay API keys are not configured "
            "(set RECONPROOF_RAZORPAY_KEY_ID / RECONPROOF_RAZORPAY_KEY_SECRET)"
        )
    return RazorpayClient(
        key_id=settings.razorpay_key_id,
        key_secret=settings.razorpay_key_secret,
        base_url=settings.razorpay_api_base_url,
        timeout=settings.razorpay_sync_timeout_seconds,
    )


@integrations_router.post("/razorpay/sync", response_model=RazorpaySyncResult)
def sync_razorpay(
    body: RazorpaySyncRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RazorpaySyncResult:
    """Pull payments and refunds for a window straight from Razorpay's API.

    Equivalent to uploading a `razorpay_payments` and a `razorpay_refunds`
    export for the same window: same schema, same dedup, same audit trail.
    """
    batch = _get_batch(db, body.batch_id)
    if body.to_time <= body.from_time:
        raise HTTPException(status_code=400, detail="to_time must be after from_time")

    try:
        client = _build_client(settings)
    except RazorpayConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    from_ts = int(body.from_time.timestamp())
    to_ts = int(body.to_time.timestamp())
    try:
        payments = client.fetch_payments(from_ts=from_ts, to_ts=to_ts)
        refunds = client.fetch_refunds(from_ts=from_ts, to_ts=to_ts)
    except (httpx.HTTPError, RazorpayAPIError) as exc:
        raise HTTPException(status_code=502, detail=f"Razorpay sync failed: {exc}") from exc

    payments_summary: SourceFileSummary | None = None
    refunds_summary: SourceFileSummary | None = None

    if payments:
        result = _ingest_or_422(
            db,
            batch=batch,
            source_kind=SourceKind.RAZORPAY_PAYMENTS,
            filename=f"razorpay-payments-{from_ts}-{to_ts}.csv",
            payload=payments_to_csv(payments),
        )
        assert result.source_file is not None
        payments_summary = _source_summary(result.source_file)

    if refunds:
        result = _ingest_or_422(
            db,
            batch=batch,
            source_kind=SourceKind.RAZORPAY_REFUNDS,
            filename=f"razorpay-refunds-{from_ts}-{to_ts}.csv",
            payload=refunds_to_csv(refunds),
        )
        assert result.source_file is not None
        refunds_summary = _source_summary(result.source_file)

    if batch.status is BatchStatus.DRAFT:
        batch.status = BatchStatus.READY

    audit_record(
        db,
        action=AuditAction.RAZORPAY_SYNC_COMPLETED,
        actor=Actor.SYSTEM,
        batch_id=batch.id,
        detail={
            "from": body.from_time.isoformat(),
            "to": body.to_time.isoformat(),
            "payments_fetched": len(payments),
            "refunds_fetched": len(refunds),
        },
        message=f"Razorpay sync fetched {len(payments)} payment(s), {len(refunds)} refund(s)",
    )
    db.commit()

    return RazorpaySyncResult(batch_id=batch.id, payments=payments_summary, refunds=refunds_summary)


@webhooks_router.post("/razorpay", response_model=WebhookIngestResult, status_code=201)
async def receive_razorpay_webhook(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    batch_id: Annotated[str, Query()],
) -> WebhookIngestResult:
    """Accept one Razorpay webhook delivery and fold it into `batch_id`.

    `batch_id` is a required query parameter — Razorpay has one static
    webhook URL per account, and the only way to route a delivery to a
    specific reconciliation batch without inventing a new routing concept is
    to put the target batch in the URL Razorpay is configured to call:
    `/api/webhooks/razorpay?batch_id=<id>`.
    """
    batch = _get_batch(db, batch_id)
    raw_body = await request.body()
    signature = request.headers.get("x-razorpay-signature")

    if not verify_webhook_signature(
        secret=settings.razorpay_webhook_secret, body=raw_body, signature=signature
    ):
        audit_record(
            db,
            action=AuditAction.WEBHOOK_SIGNATURE_REJECTED,
            actor=Actor.SYSTEM,
            batch_id=batch.id,
            detail={
                "webhook_secret_configured": bool(settings.razorpay_webhook_secret),
                "signature_header_present": bool(signature),
            },
            message="Razorpay webhook rejected: signature did not verify",
        )
        db.commit()
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    try:
        event: dict[str, Any] = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"malformed webhook payload: {exc}") from exc

    header_event_id = request.headers.get("x-razorpay-event-id")
    event_id_is_derived = header_event_id is None
    if header_event_id is not None:
        event_id = header_event_id
    else:
        # Fallback for a delivery with no id header: derive a stable key from
        # the event's own content so a retried delivery still collapses via
        # ingest_file's existing dedupe, just with a weaker guarantee than
        # Razorpay's own per-event-unique header.
        basis = (
            f"{event.get('event')}|{event.get('created_at')}|"
            f"{json.dumps(event.get('payload'), sort_keys=True)}"
        )
        event_id = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]

    result = _ingest_or_422(
        db,
        batch=batch,
        source_kind=SourceKind.WEBHOOK_EVENTS,
        filename=f"webhook-{event_id}.csv",
        payload=webhook_event_to_csv(event, event_id=event_id),
    )

    audit_record(
        db,
        action=AuditAction.WEBHOOK_RECEIVED,
        actor=Actor.SYSTEM,
        batch_id=batch.id,
        detail={
            "event": event.get("event"),
            "event_id": event_id,
            "event_id_derived": event_id_is_derived,
            "duplicate": result.duplicates_collapsed > 0,
        },
        message=f"Razorpay webhook received: {event.get('event')}",
    )
    db.commit()

    return WebhookIngestResult(
        batch_id=batch.id,
        event=event.get("event") or "",
        accepted=result.accepted > 0,
        duplicate=result.duplicates_collapsed > 0,
        source=_source_summary(result.source_file) if result.source_file else None,
    )
