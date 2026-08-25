"""Batch-level anomaly detection.

Detection is deliberately mostly deterministic. "This settlement has no bank
credit" and "this fee rate is 4% when the schedule says 2%" are rules, not
inferences, and a rule states its reason in terms a finance team can check. An
isolation forest is used only for the residual case — a record that breaks no
specific rule but sits far from the batch's joint distribution — where there is
genuinely no rule to write.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from reconproof.audit.log import record as audit_record
from reconproof.config import Settings, get_settings
from reconproof.db.models import (
    AnomalyEvent,
    ReconciliationBatch,
    ReconciliationMatch,
    SourceRecord,
)
from reconproof.domain.entities import (
    Actor,
    AuditAction,
    MatchDecision,
    MatchRelation,
    RecordKind,
)
from reconproof.domain.money import Money

if TYPE_CHECKING:
    pass

#: All-in provider cost: 2% commission plus 18% GST on the commission.
EXPECTED_FEE_RATE = 0.0236
FEE_RATE_TOLERANCE = 0.004

#: Settlements landing later than this are called out even when they reconcile.
LATE_SETTLEMENT_DAYS = 5


@dataclass(slots=True)
class Anomaly:
    kind: str
    severity: str
    summary: str
    detector: str
    score: float | None = None
    subject_record_id: str | None = None
    detail: dict[str, Any] | None = None


def detect(
    session: Session, batch: ReconciliationBatch, *, settings: Settings | None = None
) -> list[AnomalyEvent]:
    """Run every detector over a completed batch and persist what they find."""
    settings = settings or get_settings()
    records = list(
        session.execute(select(SourceRecord).where(SourceRecord.batch_id == batch.id)).scalars()
    )
    matches = list(
        session.execute(
            select(ReconciliationMatch).where(
                ReconciliationMatch.batch_id == batch.id,
                ReconciliationMatch.decision == MatchDecision.AUTO_ACCEPTED,
            )
        ).scalars()
    )

    findings: list[Anomaly] = []
    findings.extend(_missing_bank_credits(records, matches, batch.currency))
    findings.extend(_fee_rate_outliers(records))
    findings.extend(_duplicate_candidates(records, batch.currency))
    findings.extend(_late_settlements(records, matches))
    findings.extend(_distributional_outliers(records, settings))

    stored: list[AnomalyEvent] = []
    for finding in findings:
        event = AnomalyEvent(
            batch_id=batch.id,
            detector=finding.detector,
            kind=finding.kind,
            severity=finding.severity,
            score=finding.score,
            subject_record_id=finding.subject_record_id,
            summary=finding.summary,
            detail=finding.detail or {},
        )
        session.add(event)
        stored.append(event)
    session.flush()

    if stored:
        audit_record(
            session,
            action=AuditAction.ANOMALY_DETECTED,
            actor=Actor.SYSTEM,
            batch_id=batch.id,
            detail={
                "count": len(stored),
                "kinds": sorted({finding.kind for finding in findings}),
            },
            message=f"{len(stored)} anomaly finding(s) recorded",
        )
    return stored


def _missing_bank_credits(
    records: list[SourceRecord], matches: list[ReconciliationMatch], currency: str
) -> list[Anomaly]:
    traced = {
        match.left_record_id
        for match in matches
        if match.relation is MatchRelation.SETTLEMENT_TO_BANK_CREDIT
    }
    findings: list[Anomaly] = []
    for record in records:
        if record.record_kind is not RecordKind.SETTLEMENT or record.id in traced:
            continue
        findings.append(
            Anomaly(
                kind="settlement_without_bank_credit",
                severity="high",
                detector="rule:missing_bank_credit",
                subject_record_id=record.id,
                summary=(
                    f"Settlement {record.settlement_ref} of "
                    f"{Money(record.amount_subunits, record.currency)} has no confirmed bank "
                    "credit. Money the provider reports as paid has not been seen arriving."
                ),
                detail={"settlement_ref": record.settlement_ref},
            )
        )
    return findings


def _fee_rate_outliers(records: list[SourceRecord]) -> list[Anomaly]:
    findings: list[Anomaly] = []
    for record in records:
        if record.record_kind is not RecordKind.PAYMENT:
            continue
        gross = abs(record.amount_subunits)
        charged = abs(record.fee_subunits or 0) + abs(record.tax_subunits or 0)
        if not gross or not charged:
            continue
        rate = charged / gross
        if abs(rate - EXPECTED_FEE_RATE) <= FEE_RATE_TOLERANCE:
            continue
        findings.append(
            Anomaly(
                kind="fee_rate_deviation",
                severity="medium" if rate > EXPECTED_FEE_RATE else "low",
                detector="rule:fee_schedule",
                subject_record_id=record.id,
                score=round(rate, 5),
                summary=(
                    f"Payment {record.payment_ref} was charged {rate:.2%} in fees and tax "
                    f"against an expected {EXPECTED_FEE_RATE:.2%}."
                ),
                detail={"observed_rate": rate, "expected_rate": EXPECTED_FEE_RATE},
            )
        )
    return findings[:50]


def _duplicate_candidates(records: list[SourceRecord], currency: str) -> list[Anomaly]:
    """Records sharing an amount *and* a reference, which a real ledger should not."""
    grouped: dict[tuple[str, int, str], list[SourceRecord]] = defaultdict(list)
    for record in records:
        if record.record_kind is not RecordKind.BANK_CREDIT or not record.bank_ref_normalized:
            continue
        grouped[
            (record.record_kind.value, record.amount_subunits, record.bank_ref_normalized)
        ].append(record)

    findings: list[Anomaly] = []
    for (_kind, amount, reference), group in grouped.items():
        if len(group) < 2:
            continue
        findings.append(
            Anomaly(
                kind="duplicate_bank_credit",
                severity="high",
                detector="rule:duplicate_reference",
                subject_record_id=group[0].id,
                summary=(
                    f"{len(group)} bank credits share the amount "
                    f"{Money(amount, currency)} and reference {reference}. One is likely a "
                    "duplicate and must not be reconciled twice."
                ),
                detail={"record_ids": [record.id for record in group]},
            )
        )
    return findings


def _late_settlements(
    records: list[SourceRecord], matches: list[ReconciliationMatch]
) -> list[Anomaly]:
    by_id = {record.id: record for record in records}
    findings: list[Anomaly] = []
    for match in matches:
        if match.relation is not MatchRelation.SETTLEMENT_TO_BANK_CREDIT:
            continue
        settlement = by_id.get(match.left_record_id)
        credit = by_id.get(match.right_record_id)
        if not settlement or not credit:
            continue
        if settlement.occurred_at is None or credit.occurred_at is None:
            continue
        lag_days = (credit.occurred_at - settlement.occurred_at).total_seconds() / 86400
        if lag_days <= LATE_SETTLEMENT_DAYS:
            continue
        findings.append(
            Anomaly(
                kind="late_bank_credit",
                severity="medium",
                detector="rule:settlement_lag",
                subject_record_id=settlement.id,
                score=round(lag_days, 2),
                summary=(
                    f"Settlement {settlement.settlement_ref} reconciled, but the bank credit "
                    f"arrived {lag_days:.1f} days later than the settlement date."
                ),
                detail={"lag_days": lag_days},
            )
        )
    return findings[:50]


def _distributional_outliers(records: list[SourceRecord], settings: Settings) -> list[Anomaly]:
    """Isolation-forest pass over payment shape.

    Used only for what the rules cannot express. Flagged records are reported as
    "unusual", never as wrong: an outlier is a prompt to look, not a finding.
    """
    payments = [
        record
        for record in records
        if record.record_kind is RecordKind.PAYMENT and record.occurred_at is not None
    ]
    if len(payments) < 80:
        # Too few points for the contamination estimate to mean anything.
        return []

    features = np.asarray(
        [
            [
                float(abs(record.amount_subunits)),
                float((record.fee_subunits or 0) + (record.tax_subunits or 0)),
                float(record.occurred_at.hour if record.occurred_at else 0),
            ]
            for record in payments
        ],
        dtype=float,
    )
    # Log-scale the monetary columns so the forest is not dominated by scale.
    features[:, 0] = np.log1p(features[:, 0])
    features[:, 1] = np.log1p(features[:, 1])

    from sklearn.ensemble import IsolationForest

    forest = IsolationForest(
        contamination=min(max(settings.anomaly_contamination, 0.005), 0.2),
        random_state=0,
        n_estimators=100,
    )
    predictions = forest.fit_predict(features)
    scores = forest.score_samples(features)

    findings: list[Anomaly] = []
    ranked = sorted(
        (
            (score, record)
            for score, prediction, record in zip(scores, predictions, payments, strict=True)
            if prediction == -1
        ),
        key=lambda pair: pair[0],
    )
    for score, record in ranked[:15]:
        findings.append(
            Anomaly(
                kind="unusual_payment_shape",
                severity="low",
                detector="isolation_forest",
                subject_record_id=record.id,
                score=round(float(score), 4),
                summary=(
                    f"Payment {record.payment_ref} of "
                    f"{Money(record.amount_subunits, record.currency)} is unusual for this "
                    "batch on amount, fee and time-of-day taken together. No specific rule "
                    "was broken."
                ),
                detail={"isolation_score": float(score)},
            )
        )
    return findings
