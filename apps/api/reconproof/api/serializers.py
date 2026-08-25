"""ORM to response-model conversion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from reconproof.api.schemas import (
    Amount,
    AuditEntry,
    CandidateSummary,
    CounterfactualSummary,
    EvidenceSummary,
    FeatureContribution,
    GraphEdge,
    GraphNode,
    InvariantSummary,
    MatchSummary,
    RecordSummary,
)
from reconproof.matching.features import describe_feature

if TYPE_CHECKING:
    from reconproof.db.models import (
        AccountingCheck,
        AuditEvent,
        EvidenceItem,
        MatchCandidate,
        ReconciliationMatch,
        SourceRecord,
    )
    from reconproof.matching.scoring import CalibratedMatchScorer


def record_reference(record: SourceRecord) -> str | None:
    """The most identifying reference this record carries."""
    return (
        record.settlement_ref
        or record.payment_ref
        or record.order_ref
        or record.bank_ref
        or record.external_id
    )


def serialize_record(record: SourceRecord) -> RecordSummary:
    return RecordSummary(
        id=record.id,
        source_kind=record.source_kind.value,
        record_kind=record.record_kind.value,
        external_id=record.external_id,
        reference=record_reference(record),
        amount=Amount.of(record.amount_subunits, record.currency),
        occurred_at=record.occurred_at,
        timestamp_is_date_only=record.timestamp_is_date_only,
        description=record.description,
        counterparty=record.counterparty,
        payment_method=record.payment_method.value,
        status=record.status.value,
        raw=record.raw or {},
    )


def serialize_evidence(item: EvidenceItem) -> EvidenceSummary:
    return EvidenceSummary(
        id=item.id,
        kind=item.kind,
        statement=item.statement,
        supports=item.supports,
        weight=item.weight,
        produced_by=item.produced_by,
        detail=item.detail or {},
    )


def serialize_check(check: AccountingCheck) -> InvariantSummary:
    return InvariantSummary(
        invariant=check.invariant,
        passed=check.passed,
        message=check.message,
        detail=check.detail or {},
    )


def serialize_match(
    match: ReconciliationMatch, left: SourceRecord, right: SourceRecord
) -> MatchSummary:
    return MatchSummary(
        id=match.id,
        relation=match.relation.value,
        decision=match.decision.value,
        method=match.method.value,
        score=match.score,
        risk=match.risk,
        allocated=Amount.of(match.allocated_subunits, match.currency),
        rationale=match.rationale,
        invariants_passed=match.invariants_passed or [],
        invariants_failed=[name for name in (match.invariants_failed or []) if name],
        left=serialize_record(left),
        right=serialize_record(right),
    )


def serialize_candidate(
    candidate: MatchCandidate,
    left: SourceRecord,
    right: SourceRecord,
    evidence: list[EvidenceItem],
    checks: list[AccountingCheck],
    scorer: CalibratedMatchScorer | None = None,
) -> CandidateSummary:
    features = candidate.features or {}
    contributions: list[FeatureContribution] = []
    if scorer is not None and features:
        # A candidate this old model never scored (e.g. a deterministic exact
        # match with no learned features attached) has nothing to attribute —
        # contributions_for would just report every feature at its default 0.0,
        # which is misleading rather than merely empty, so skip it entirely.
        contributions = [FeatureContribution(**row) for row in scorer.contributions_for(features)]
    return CandidateSummary(
        id=candidate.id,
        relation=candidate.relation.value,
        generator=candidate.generator,
        score=candidate.score,
        risk=candidate.risk,
        features=features,
        feature_contributions=contributions,
        left=serialize_record(left),
        right=serialize_record(right),
        evidence=[serialize_evidence(item) for item in evidence],
        invariants=[serialize_check(check) for check in checks],
    )


def serialize_audit(event: AuditEvent) -> AuditEntry:
    return AuditEntry(
        id=event.id,
        sequence=event.sequence,
        action=event.action.value,
        actor=event.actor.value,
        actor_detail=event.actor_detail,
        subject_type=event.subject_type,
        subject_id=event.subject_id,
        message=event.message,
        detail=event.detail or {},
        input_sha256=event.input_sha256,
        created_at=event.created_at,
    )


def serialize_graph_node(record: SourceRecord) -> GraphNode:
    return GraphNode(
        id=record.id,
        kind=record.record_kind.value,
        source=record.source_kind.value,
        amount=Amount.of(record.amount_subunits, record.currency),
        reference=record_reference(record),
        occurred_at=record.occurred_at.isoformat() if record.occurred_at else None,
    )


def serialize_graph_edge(match: ReconciliationMatch) -> GraphEdge:
    return GraphEdge(
        source=match.left_record_id,
        target=match.right_record_id,
        relation=match.relation.value,
        decision=match.decision.value,
        method=match.method.value,
        score=match.score,
        risk=match.risk,
        allocated=Amount.of(match.allocated_subunits, match.currency),
        blocking=[name for name in (match.invariants_failed or []) if name],
    )


#: Features whose value a reviewer can act on. A counterfactual naming
#: ``amount_log_diff`` would be technically true and operationally useless.
ACTIONABLE_FEATURES: tuple[str, ...] = (
    "reference_exact",
    "reference_containment",
    "reference_tail_match",
    "within_window",
    "is_sole_candidate",
    "amount_exact",
    "currency_match",
)


def build_counterfactual(features: dict[str, float], blocking: list[str]) -> CounterfactualSummary:
    """Describe what would have to be true for this link to be acceptable.

    Derived from the actual feature values and failing checks, not generated
    prose. A reviewer can verify each statement against the records in front of
    them, which is the difference between an explanation and a guess.
    """
    statements: list[str] = []
    for name in ACTIONABLE_FEATURES:
        value = features.get(name)
        if value is None:
            continue
        if value <= 0.0:
            statements.append(_missing_condition(name))
    if features.get("amount_unit_confusion", 0.0) > 0:
        statements.append(
            "The two amounts differ by exactly 100x; confirm whether one column is in "
            "paise and the other in rupees."
        )
    competing = features.get("competing_candidates")
    if competing and competing > 1:
        statements.append(
            f"{int(competing)} records compete for this link; ruling out "
            f"{int(competing) - 1} of them would make the attribution unique."
        )
    return CounterfactualSummary(statements=statements, blocking=blocking)


def _missing_condition(name: str) -> str:
    conditions = {
        "reference_exact": "The references would need to match exactly.",
        "reference_containment": (
            "One record's reference would need to appear in the other's narration."
        ),
        "reference_tail_match": "The trailing reference digits would need to agree.",
        "within_window": (
            "The two dates would need to fall inside the expected settlement window."
        ),
        "is_sole_candidate": "No other record could be competing for this link.",
        "amount_exact": "The amounts would need to be identical.",
        "currency_match": "Both records would need to be in the same currency.",
    }
    return conditions.get(name, describe_feature(name, 0.0))
