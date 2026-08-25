"""Distribution drift between batches.

Drift matters here for one specific reason: the automation threshold carries a
risk guarantee that was established on a particular input distribution. When the
distribution moves, that evidence becomes less applicable, and the honest
response is to automate *less* until the threshold is re-established.

So a detection tightens the risk budget. It never loosens it. A drift detector
that could increase automation would be a mechanism for silently voiding the
guarantee it exists to protect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from reconproof.audit.log import record as audit_record
from reconproof.config import Settings, get_settings
from reconproof.db.models import DriftReport, ReconciliationBatch, SourceRecord
from reconproof.domain.entities import Actor, AuditAction, RecordKind

#: Buckets used for the population-stability comparison.
BIN_COUNT = 10

#: Conventional PSI reading: below 0.1 is stable, 0.1-0.25 is a moderate shift,
#: above 0.25 is a significant one.
PSI_MODERATE = 0.1


@dataclass(slots=True)
class FeatureDrift:
    feature: str
    psi: float
    reference_mean: float
    current_mean: float

    @property
    def verdict(self) -> str:
        if self.psi < PSI_MODERATE:
            return "stable"
        return "moderate" if self.psi < 0.25 else "significant"

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "psi": round(self.psi, 4),
            "reference_mean": round(self.reference_mean, 4),
            "current_mean": round(self.current_mean, 4),
            "verdict": self.verdict,
        }


def population_stability_index(
    reference: np.ndarray, current: np.ndarray, bins: int = BIN_COUNT
) -> float:
    """PSI between two samples.

    Bin edges come from the reference quantiles so the comparison is against the
    distribution the threshold was calibrated on. A small epsilon prevents an
    empty bucket from producing an infinite index.
    """
    if reference.size == 0 or current.size == 0:
        return 0.0
    quantiles = np.linspace(0, 100, bins + 1)
    edges = np.unique(np.percentile(reference, quantiles))
    if edges.size < 3:
        return 0.0
    reference_counts, _ = np.histogram(reference, bins=edges)
    current_counts, _ = np.histogram(current, bins=edges)
    epsilon = 1e-6
    reference_share = reference_counts / max(reference_counts.sum(), 1) + epsilon
    current_share = current_counts / max(current_counts.sum(), 1) + epsilon
    return float(
        np.sum((current_share - reference_share) * np.log(current_share / reference_share))
    )


def _feature_matrix(session: Session, batch_id: str) -> dict[str, np.ndarray]:
    """Extract the input characteristics whose movement would matter."""
    records = list(
        session.execute(select(SourceRecord).where(SourceRecord.batch_id == batch_id)).scalars()
    )
    payments = [record for record in records if record.record_kind is RecordKind.PAYMENT]
    credits = [record for record in records if record.record_kind is RecordKind.BANK_CREDIT]

    fee_rates = [
        ((record.fee_subunits or 0) + (record.tax_subunits or 0)) / abs(record.amount_subunits)
        for record in payments
        if record.amount_subunits
    ]
    narration_lengths = [float(len(record.description or "")) for record in credits]
    reference_lengths = [float(len(record.bank_ref_normalized or "")) for record in credits]
    return {
        "payment_amount_log": np.log1p(
            np.asarray([abs(record.amount_subunits) for record in payments], dtype=float)
        ),
        "fee_rate": np.asarray(fee_rates, dtype=float),
        "bank_narration_length": np.asarray(narration_lengths, dtype=float),
        "bank_reference_length": np.asarray(reference_lengths, dtype=float),
    }


def evaluate(
    session: Session,
    batch: ReconciliationBatch,
    *,
    reference_batch: ReconciliationBatch | None = None,
    settings: Settings | None = None,
) -> DriftReport | None:
    """Compare *batch* against a reference batch and record the result.

    Returns ``None`` when there is no earlier batch to compare against. A first
    batch has no drift by definition, and inventing a baseline would produce a
    number with no meaning.
    """
    settings = settings or get_settings()
    if reference_batch is None:
        reference_batch = (
            session.execute(
                select(ReconciliationBatch)
                .where(
                    ReconciliationBatch.id != batch.id,
                    ReconciliationBatch.created_at < batch.created_at,
                )
                .order_by(ReconciliationBatch.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
    if reference_batch is None:
        return None

    reference_features = _feature_matrix(session, reference_batch.id)
    current_features = _feature_matrix(session, batch.id)

    drifts: list[FeatureDrift] = []
    for name, reference in reference_features.items():
        current = current_features.get(name, np.asarray([], dtype=float))
        if reference.size < 30 or current.size < 30:
            # Too little data to distinguish a shift from sampling noise.
            continue
        drifts.append(
            FeatureDrift(
                feature=name,
                psi=population_stability_index(reference, current),
                reference_mean=float(np.mean(reference)),
                current_mean=float(np.mean(current)),
            )
        )

    if not drifts:
        return None

    max_psi = max(drift.psi for drift in drifts)
    detected = max_psi >= settings.drift_psi_threshold
    worst = max(drifts, key=lambda drift: drift.psi)

    report = DriftReport(
        batch_id=batch.id,
        reference_batch_id=reference_batch.id,
        drift_detected=detected,
        max_psi=max_psi,
        features={drift.feature: drift.to_dict() for drift in drifts},
        triggered_restriction=detected,
        summary=(
            (
                f"Input distribution has shifted against batch '{reference_batch.name}'. "
                f"The largest movement is in {worst.feature} (PSI {worst.psi:.3f}). "
                "Automation has been tightened until the threshold is re-established on "
                "current data."
            )
            if detected
            else (
                f"No material drift against batch '{reference_batch.name}'. Largest movement "
                f"is {worst.feature} at PSI {worst.psi:.3f}."
            )
        ),
    )
    session.add(report)

    if detected:
        batch.automation_restricted = True
        batch.restriction_reason = report.summary
        audit_record(
            session,
            action=AuditAction.DRIFT_DETECTED,
            actor=Actor.SYSTEM,
            batch_id=batch.id,
            detail={
                "max_psi": max_psi,
                "reference_batch_id": reference_batch.id,
                "features": {drift.feature: drift.psi for drift in drifts},
            },
            message=report.summary,
        )
        audit_record(
            session,
            action=AuditAction.AUTOMATION_RESTRICTED,
            actor=Actor.SYSTEM,
            batch_id=batch.id,
            detail={"risk_tightening_factor": settings.drift_risk_tightening},
            message=(
                "Risk budget tightened by a factor of "
                f"{settings.drift_risk_tightening} following drift detection"
            ),
        )
    session.flush()
    return report
