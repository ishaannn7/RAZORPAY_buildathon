"""Anomaly and drift endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from reconproof.api.schemas import AnomalySummary, DriftSummary
from reconproof.config import Settings, get_settings
from reconproof.db.models import AnomalyEvent, DriftReport, ReconciliationBatch
from reconproof.db.session import get_db
from reconproof.monitoring import anomaly, drift

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


def _get_batch(db: Session, batch_id: str) -> ReconciliationBatch:
    batch = db.get(ReconciliationBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"batch {batch_id} not found")
    return batch


@router.post("/{batch_id}/analyze")
def analyze(
    batch_id: str,
    db: Annotated[Session, Depends(get_db)],
    config: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Run anomaly detection and drift comparison for a completed batch."""
    batch = _get_batch(db, batch_id)
    # Re-analysis replaces prior findings so the page cannot show two
    # generations of results at once.
    for existing in db.execute(
        select(AnomalyEvent).where(AnomalyEvent.batch_id == batch.id)
    ).scalars():
        db.delete(existing)
    db.flush()

    anomalies = anomaly.detect(db, batch, settings=config)
    report = drift.evaluate(db, batch, settings=config)
    db.commit()
    return {
        "anomalies": len(anomalies),
        "drift_detected": bool(report and report.drift_detected),
        "max_psi": report.max_psi if report else None,
        "drift_summary": report.summary if report else "No earlier batch to compare against.",
    }


@router.get("/{batch_id}/anomalies", response_model=list[AnomalySummary])
def list_anomalies(batch_id: str, db: Annotated[Session, Depends(get_db)]) -> list[AnomalySummary]:
    _get_batch(db, batch_id)
    #: Severity is stored as text, so ordering is done in Python to keep the
    #: intended high/medium/low sequence rather than alphabetical.
    ranking = {"high": 0, "medium": 1, "low": 2}
    events = list(
        db.execute(select(AnomalyEvent).where(AnomalyEvent.batch_id == batch_id)).scalars()
    )
    events.sort(key=lambda event: (ranking.get(event.severity, 9), event.kind))
    return [AnomalySummary.model_validate(event) for event in events]


@router.get("/{batch_id}/drift", response_model=list[DriftSummary])
def list_drift(batch_id: str, db: Annotated[Session, Depends(get_db)]) -> list[DriftSummary]:
    _get_batch(db, batch_id)
    reports = list(
        db.execute(
            select(DriftReport)
            .where(DriftReport.batch_id == batch_id)
            .order_by(DriftReport.created_at.desc())
        ).scalars()
    )
    return [DriftSummary.model_validate(report) for report in reports]
