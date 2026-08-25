"""Canonical vocabulary for the reconciliation domain.

Source files arrive with wildly different column names and conventions. Every
row is normalized into one of the :class:`RecordKind` values below before any
matching happens, so the matching layer never sees a bank-specific quirk.
"""

from __future__ import annotations

from enum import StrEnum


class RecordKind(StrEnum):
    """The canonical entity a normalized source row represents."""

    ORDER = "order"
    PAYMENT = "payment"
    REFUND = "refund"
    SETTLEMENT = "settlement"
    BANK_CREDIT = "bank_credit"
    FEE = "fee"
    TAX = "tax"
    ADJUSTMENT = "adjustment"
    EVENT = "event"


class SourceKind(StrEnum):
    """Where a batch of records came from."""

    ORDER_LEDGER = "order_ledger"
    RAZORPAY_PAYMENTS = "razorpay_payments"
    RAZORPAY_REFUNDS = "razorpay_refunds"
    SETTLEMENT_REPORT = "settlement_report"
    BANK_STATEMENT = "bank_statement"
    FEE_REPORT = "fee_report"
    WEBHOOK_EVENTS = "webhook_events"


#: Which canonical entity each source kind is expected to yield.
SOURCE_RECORD_KIND: dict[SourceKind, RecordKind] = {
    SourceKind.ORDER_LEDGER: RecordKind.ORDER,
    SourceKind.RAZORPAY_PAYMENTS: RecordKind.PAYMENT,
    SourceKind.RAZORPAY_REFUNDS: RecordKind.REFUND,
    SourceKind.SETTLEMENT_REPORT: RecordKind.SETTLEMENT,
    SourceKind.BANK_STATEMENT: RecordKind.BANK_CREDIT,
    SourceKind.FEE_REPORT: RecordKind.FEE,
    SourceKind.WEBHOOK_EVENTS: RecordKind.EVENT,
}


class PaymentMethod(StrEnum):
    UPI = "upi"
    CARD = "card"
    NETBANKING = "netbanking"
    WALLET = "wallet"
    EMI = "emi"
    UNKNOWN = "unknown"


class RecordStatus(StrEnum):
    """Lifecycle status carried on the source row, where the source provides one."""

    CREATED = "created"
    ATTEMPTED = "attempted"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"
    SETTLED = "settled"
    UNKNOWN = "unknown"


class MatchRelation(StrEnum):
    """The financial meaning of a link between two canonical records."""

    ORDER_TO_PAYMENT = "order_to_payment"
    PAYMENT_TO_REFUND = "payment_to_refund"
    PAYMENT_TO_SETTLEMENT = "payment_to_settlement"
    REFUND_TO_SETTLEMENT = "refund_to_settlement"
    SETTLEMENT_TO_BANK_CREDIT = "settlement_to_bank_credit"
    FEE_TO_SETTLEMENT = "fee_to_settlement"


#: The ordered (source, target) record kinds valid for each relation. Candidate
#: generation refuses to propose a pair that does not appear here, which keeps
#: structurally impossible links out of the graph entirely.
RELATION_ENDPOINTS: dict[MatchRelation, tuple[RecordKind, RecordKind]] = {
    MatchRelation.ORDER_TO_PAYMENT: (RecordKind.ORDER, RecordKind.PAYMENT),
    MatchRelation.PAYMENT_TO_REFUND: (RecordKind.PAYMENT, RecordKind.REFUND),
    MatchRelation.PAYMENT_TO_SETTLEMENT: (RecordKind.PAYMENT, RecordKind.SETTLEMENT),
    MatchRelation.REFUND_TO_SETTLEMENT: (RecordKind.REFUND, RecordKind.SETTLEMENT),
    MatchRelation.SETTLEMENT_TO_BANK_CREDIT: (RecordKind.SETTLEMENT, RecordKind.BANK_CREDIT),
    MatchRelation.FEE_TO_SETTLEMENT: (RecordKind.FEE, RecordKind.SETTLEMENT),
}


#: Relations where many source records legitimately roll up into one target.
#: A settlement aggregates many payments; a bank credit corresponds to exactly
#: one settlement. Encoding this drives the assignment layer's capacity limits.
MANY_TO_ONE_RELATIONS: frozenset[MatchRelation] = frozenset(
    {
        MatchRelation.PAYMENT_TO_SETTLEMENT,
        MatchRelation.REFUND_TO_SETTLEMENT,
        MatchRelation.FEE_TO_SETTLEMENT,
        MatchRelation.PAYMENT_TO_REFUND,
    }
)


class MatchDecision(StrEnum):
    """The outcome the pipeline reached for a candidate link."""

    AUTO_ACCEPTED = "auto_accepted"
    HUMAN_REVIEW = "human_review"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"


class MatchMethod(StrEnum):
    """Which stage produced a decision. Recorded on every match for audit."""

    EXACT_REFERENCE = "exact_reference"
    EXACT_COMPOSITE = "exact_composite"
    CALIBRATED_MODEL = "calibrated_model"
    GLOBAL_ASSIGNMENT = "global_assignment"
    AGENT_RECOMMENDATION = "agent_recommendation"
    HUMAN_RESOLUTION = "human_resolution"


class ExceptionCategory(StrEnum):
    """Why a record could not be automatically reconciled."""

    NO_CANDIDATE = "no_candidate"
    AMBIGUOUS_CANDIDATES = "ambiguous_candidates"
    MISSING_REFERENCE = "missing_reference"
    AMOUNT_MISMATCH = "amount_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    DUPLICATE_RECORD = "duplicate_record"
    OVER_REFUND = "over_refund"
    UNBALANCED_ALLOCATION = "unbalanced_allocation"
    LATE_SETTLEMENT = "late_settlement"
    MISSING_BANK_CREDIT = "missing_bank_credit"
    FEE_DISCREPANCY = "fee_discrepancy"
    TAX_DISCREPANCY = "tax_discrepancy"
    UNIT_CONFUSION = "unit_confusion"
    LOW_CONFIDENCE = "low_confidence"
    POLICY_BLOCKED = "policy_blocked"


class ExceptionStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    RESOLVED = "resolved"
    WRITTEN_OFF = "written_off"


class BatchStatus(StrEnum):
    DRAFT = "draft"
    VALIDATING = "validating"
    VALIDATION_FAILED = "validation_failed"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentPhase(StrEnum):
    """States of the bounded investigation workflow."""

    TRIAGE = "triage"
    PLAN = "plan"
    GATHER_EVIDENCE = "gather_evidence"
    GENERATE_HYPOTHESES = "generate_hypotheses"
    VERIFY = "verify"
    SELF_CRITIQUE = "self_critique"
    RECOMMEND = "recommend"
    ABSTAIN = "abstain"
    HUMAN_REVIEW = "human_review"
    FAILED = "failed"


class AgentOutcome(StrEnum):
    RECOMMENDED = "recommended"
    ABSTAINED = "abstained"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TOOL_FAILURE = "tool_failure"
    INVALID_OUTPUT = "invalid_output"
    PROVIDER_UNAVAILABLE = "provider_unavailable"


class AuditAction(StrEnum):
    BATCH_CREATED = "batch_created"
    SOURCE_UPLOADED = "source_uploaded"
    SOURCE_REJECTED = "source_rejected"
    DUPLICATE_UPLOAD_IGNORED = "duplicate_upload_ignored"
    VALIDATION_COMPLETED = "validation_completed"
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    MATCH_AUTO_ACCEPTED = "match_auto_accepted"
    MATCH_SENT_TO_REVIEW = "match_sent_to_review"
    MATCH_REJECTED_BY_INVARIANT = "match_rejected_by_invariant"
    DUPLICATE_EVENT_IGNORED = "duplicate_event_ignored"
    EXCEPTION_OPENED = "exception_opened"
    AGENT_RUN_STARTED = "agent_run_started"
    AGENT_TOOL_CALLED = "agent_tool_called"
    AGENT_TOOL_DENIED = "agent_tool_denied"
    AGENT_RECOMMENDED = "agent_recommended"
    AGENT_ABSTAINED = "agent_abstained"
    AGENT_OUTPUT_REJECTED = "agent_output_rejected"
    HUMAN_APPROVED = "human_approved"
    HUMAN_OVERRODE = "human_overrode"
    HUMAN_MARKED_UNRESOLVED = "human_marked_unresolved"
    ANOMALY_DETECTED = "anomaly_detected"
    DRIFT_DETECTED = "drift_detected"
    AUTOMATION_RESTRICTED = "automation_restricted"
    MODEL_TRAINED = "model_trained"
    MODEL_PROMOTION_APPROVED = "model_promotion_approved"
    MODEL_PROMOTION_REJECTED = "model_promotion_rejected"
    EXPORT_GENERATED = "export_generated"


class Actor(StrEnum):
    """Who performed an audited action. Agents are never recorded as humans."""

    SYSTEM = "system"
    PIPELINE = "pipeline"
    AGENT = "agent"
    HUMAN = "human"
