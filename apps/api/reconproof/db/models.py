"""Persistence schema.

Two invariants shape this schema and are enforced at the ORM layer as well as
in application code:

1. ``SourceRecord`` rows are immutable once written. Normalization, matching and
   human resolution all produce *new* rows that reference the original; nothing
   edits an uploaded value. This is what makes an audit replay meaningful.
2. ``AuditEvent`` is append-only. There is no update or delete path.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reconproof.db.base import Base, IdMixin, StrEnumType, TimestampMixin, UtcDateTime
from reconproof.domain.entities import (
    Actor,
    AgentOutcome,
    AgentPhase,
    AuditAction,
    BatchStatus,
    ExceptionCategory,
    ExceptionStatus,
    MatchDecision,
    MatchMethod,
    MatchRelation,
    PaymentMethod,
    RecordKind,
    RecordStatus,
    SourceKind,
)

# ---------------------------------------------------------------------------
# Batches and sources
# ---------------------------------------------------------------------------


class ReconciliationBatch(Base, IdMixin, TimestampMixin):
    __tablename__ = "reconciliation_batches"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[BatchStatus] = mapped_column(
        StrEnumType(BatchStatus, 32), default=BatchStatus.DRAFT, nullable=False, index=True
    )
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    period_start: Mapped[datetime | None] = mapped_column(UtcDateTime)
    period_end: Mapped[datetime | None] = mapped_column(UtcDateTime)

    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    # Set when the run was executed with automation thresholds tightened by
    # drift detection, so a reviewer can explain a coverage drop after the fact.
    automation_restricted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    restriction_reason: Mapped[str | None] = mapped_column(Text)

    policy_version_id: Mapped[str | None] = mapped_column(ForeignKey("policy_versions.id"))
    model_version_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id"))

    #: Seed used by the synthetic generator, when this batch came from one.
    #: Present so a reported metric can be reproduced exactly.
    dataset_seed: Mapped[int | None] = mapped_column(Integer)

    source_files: Mapped[list[SourceFile]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )
    records: Mapped[list[SourceRecord]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class SourceFile(Base, IdMixin, TimestampMixin):
    __tablename__ = "source_files"
    __table_args__ = (
        # The same file content may not be ingested twice into one batch. This
        # is the first line of duplicate-import defence; the second is the
        # per-record dedupe key below.
        UniqueConstraint("batch_id", "content_sha256", name="uq_source_files_batch_content"),
    )

    batch_id: Mapped[str] = mapped_column(ForeignKey("reconciliation_batches.id"), index=True)
    source_kind: Mapped[SourceKind] = mapped_column(StrEnumType(SourceKind, 40), nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    stored_path: Mapped[str] = mapped_column(String(1000), nullable=False)

    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    accepted_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejected_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    validation_errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    detected_columns: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)

    batch: Mapped[ReconciliationBatch] = relationship(back_populates="source_files")


class SourceRecord(Base, IdMixin, TimestampMixin):
    """A normalized row. Immutable after insert.

    ``raw`` retains the original values exactly as parsed so a reviewer can see
    what the source said, not merely what normalization made of it.
    """

    __tablename__ = "source_records"
    __table_args__ = (
        # Idempotent ingestion: a replayed webhook or re-sent settlement row
        # collapses onto the same natural key instead of double-counting.
        UniqueConstraint("batch_id", "dedupe_key", name="uq_source_records_batch_dedupe"),
        CheckConstraint("amount_subunits IS NOT NULL", name="amount_required"),
        Index("ix_source_records_kind_amount", "batch_id", "record_kind", "amount_subunits"),
        Index("ix_source_records_kind_time", "batch_id", "record_kind", "occurred_at"),
    )

    batch_id: Mapped[str] = mapped_column(ForeignKey("reconciliation_batches.id"), index=True)
    source_file_id: Mapped[str | None] = mapped_column(ForeignKey("source_files.id"), index=True)
    source_kind: Mapped[SourceKind] = mapped_column(StrEnumType(SourceKind, 40), nullable=False)
    record_kind: Mapped[RecordKind] = mapped_column(
        StrEnumType(RecordKind, 24), nullable=False, index=True
    )

    source_row_number: Mapped[int | None] = mapped_column(Integer)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)

    # ---- identifiers ------------------------------------------------------
    external_id: Mapped[str | None] = mapped_column(String(120), index=True)
    order_ref: Mapped[str | None] = mapped_column(String(120), index=True)
    payment_ref: Mapped[str | None] = mapped_column(String(120), index=True)
    settlement_ref: Mapped[str | None] = mapped_column(String(120), index=True)
    bank_ref: Mapped[str | None] = mapped_column(String(120), index=True)
    #: ``bank_ref`` with formatting removed and case folded, for join and
    #: similarity work. Bank statements truncate and reformat references, so the
    #: raw value is unreliable as a key on its own.
    bank_ref_normalized: Mapped[str | None] = mapped_column(String(120), index=True)

    # ---- amounts (integer minor units) ------------------------------------
    amount_subunits: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    fee_subunits: Mapped[int | None] = mapped_column(Integer)
    tax_subunits: Mapped[int | None] = mapped_column(Integer)
    net_subunits: Mapped[int | None] = mapped_column(Integer)
    gross_subunits: Mapped[int | None] = mapped_column(Integer)
    #: A settlement reports the total refunds it absorbed but not which refunds
    #: they were. Stored so candidate generation can constrain proposals to the
    #: portion of that total still unattributed.
    refund_total_subunits: Mapped[int | None] = mapped_column(Integer)

    # ---- descriptive ------------------------------------------------------
    occurred_at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)
    #: True when the source supplied only a date. ``occurred_at`` is then
    #: midnight by convention, and any ordering assertion against another record
    #: on the same day is unsupported by the data.
    timestamp_is_date_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: Case-folded, punctuation-stripped ``description`` used for similarity.
    description_normalized: Mapped[str | None] = mapped_column(Text)
    counterparty: Mapped[str | None] = mapped_column(String(300))
    payment_method: Mapped[PaymentMethod] = mapped_column(
        StrEnumType(PaymentMethod, 24), default=PaymentMethod.UNKNOWN, nullable=False
    )
    status: Mapped[RecordStatus] = mapped_column(
        StrEnumType(RecordStatus, 24), default=RecordStatus.UNKNOWN, nullable=False
    )

    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # ---- ground truth (synthetic datasets only) ---------------------------
    #: Populated by the synthetic generator so the evaluation harness can score
    #: the pipeline. Never read by matching code: doing so would leak labels.
    truth_group: Mapped[str | None] = mapped_column(String(64), index=True)
    truth_corruptions: Mapped[list[str]] = mapped_column(JSON, default=list)

    batch: Mapped[ReconciliationBatch] = relationship(back_populates="records")


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


class MatchCandidate(Base, IdMixin, TimestampMixin):
    """A proposed link, scored but not yet decided."""

    __tablename__ = "match_candidates"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "left_record_id",
            "right_record_id",
            "relation",
            name="uq_match_candidates_pair",
        ),
        Index("ix_match_candidates_batch_score", "batch_id", "score"),
    )

    batch_id: Mapped[str] = mapped_column(ForeignKey("reconciliation_batches.id"), index=True)
    left_record_id: Mapped[str] = mapped_column(ForeignKey("source_records.id"), index=True)
    right_record_id: Mapped[str] = mapped_column(ForeignKey("source_records.id"), index=True)
    relation: Mapped[MatchRelation] = mapped_column(StrEnumType(MatchRelation, 40), nullable=False)

    generator: Mapped[str] = mapped_column(String(60), nullable=False)
    features: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    score: Mapped[float | None] = mapped_column(Float, index=True)
    #: Conformal risk bound: an upper estimate of the probability this link is
    #: wrong. Acceptance is gated on this rather than on ``score`` alone.
    risk: Mapped[float | None] = mapped_column(Float)
    calibrated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ReconciliationMatch(Base, IdMixin, TimestampMixin):
    """A decided link. One row per accepted or reviewed relationship."""

    __tablename__ = "reconciliation_matches"
    __table_args__ = (Index("ix_matches_batch_decision", "batch_id", "decision"),)

    batch_id: Mapped[str] = mapped_column(ForeignKey("reconciliation_batches.id"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("match_candidates.id"))
    left_record_id: Mapped[str] = mapped_column(ForeignKey("source_records.id"), index=True)
    right_record_id: Mapped[str] = mapped_column(ForeignKey("source_records.id"), index=True)
    relation: Mapped[MatchRelation] = mapped_column(StrEnumType(MatchRelation, 40), nullable=False)

    decision: Mapped[MatchDecision] = mapped_column(
        StrEnumType(MatchDecision, 24), nullable=False, index=True
    )
    method: Mapped[MatchMethod] = mapped_column(StrEnumType(MatchMethod, 32), nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    risk: Mapped[float | None] = mapped_column(Float)

    #: The portion of the left record's amount attributed to the right record.
    #: For a many-to-one settlement this is the payment's share, not the whole
    #: settlement, which is what makes the balance assertion checkable.
    allocated_subunits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    model_version_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id"))
    policy_version_id: Mapped[str | None] = mapped_column(ForeignKey("policy_versions.id"))
    rationale: Mapped[str | None] = mapped_column(Text)
    invariants_passed: Mapped[list[str]] = mapped_column(JSON, default=list)
    invariants_failed: Mapped[list[str]] = mapped_column(JSON, default=list)


class EvidenceItem(Base, IdMixin, TimestampMixin):
    """A single citable fact.

    Every explanation surfaced in the UI or produced by an agent must cite
    evidence rows that exist here. The verifier rejects any recommendation
    referencing an unknown evidence id, which is how hallucinated citations are
    caught rather than trusted.
    """

    __tablename__ = "evidence_items"
    __table_args__ = (Index("ix_evidence_batch_subject", "batch_id", "subject_record_id"),)

    batch_id: Mapped[str] = mapped_column(ForeignKey("reconciliation_batches.id"), index=True)
    subject_record_id: Mapped[str | None] = mapped_column(ForeignKey("source_records.id"))
    related_record_id: Mapped[str | None] = mapped_column(ForeignKey("source_records.id"))
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("match_candidates.id"))
    exception_id: Mapped[str | None] = mapped_column(ForeignKey("exceptions.id"), index=True)

    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    supports: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    produced_by: Mapped[str] = mapped_column(String(60), default="pipeline", nullable=False)


class AccountingCheck(Base, IdMixin, TimestampMixin):
    """The recorded outcome of one invariant evaluation."""

    __tablename__ = "accounting_checks"

    batch_id: Mapped[str] = mapped_column(ForeignKey("reconciliation_batches.id"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("match_candidates.id"))
    exception_id: Mapped[str | None] = mapped_column(ForeignKey("exceptions.id"))
    invariant: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    message: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------------------
# Exceptions and human review
# ---------------------------------------------------------------------------


class ReconciliationException(Base, IdMixin, TimestampMixin):
    __tablename__ = "exceptions"
    __table_args__ = (
        Index("ix_exceptions_batch_status", "batch_id", "status"),
        Index("ix_exceptions_batch_category", "batch_id", "category"),
    )

    batch_id: Mapped[str] = mapped_column(ForeignKey("reconciliation_batches.id"), index=True)
    subject_record_id: Mapped[str] = mapped_column(ForeignKey("source_records.id"), index=True)
    category: Mapped[ExceptionCategory] = mapped_column(
        StrEnumType(ExceptionCategory, 40), nullable=False
    )
    status: Mapped[ExceptionStatus] = mapped_column(
        StrEnumType(ExceptionStatus, 24), default=ExceptionStatus.OPEN, nullable=False
    )
    #: Unresolved value carried by this exception. Summing this column over open
    #: exceptions must equal the batch's unexplained amount, which is the
    #: guarantee behind "100% of unresolved value is represented".
    amount_subunits: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    best_score: Mapped[float | None] = mapped_column(Float)
    best_risk: Mapped[float | None] = mapped_column(Float)
    blocking_invariants: Mapped[list[str]] = mapped_column(JSON, default=list)
    counterfactual: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    explanation: Mapped[str | None] = mapped_column(Text)
    explanation_provider: Mapped[str | None] = mapped_column(String(60))


class HumanResolution(Base, IdMixin, TimestampMixin):
    __tablename__ = "human_resolutions"

    exception_id: Mapped[str] = mapped_column(ForeignKey("exceptions.id"), index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("reconciliation_batches.id"), index=True)
    reviewer: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    chosen_candidate_id: Mapped[str | None] = mapped_column(ForeignKey("match_candidates.id"))
    chosen_record_id: Mapped[str | None] = mapped_column(ForeignKey("source_records.id"))
    reclassified_category: Mapped[ExceptionCategory | None] = mapped_column(
        StrEnumType(ExceptionCategory, 40)
    )
    notes: Mapped[str | None] = mapped_column(Text)
    #: True when the reviewer picked a link the agent did not recommend. Tracked
    #: separately because override rate is the honest measure of agent quality.
    overrode_agent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    agent_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))
    #: Whether this resolution is eligible as a training label.
    label_usable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class AgentRun(Base, IdMixin, TimestampMixin):
    __tablename__ = "agent_runs"

    batch_id: Mapped[str] = mapped_column(ForeignKey("reconciliation_batches.id"), index=True)
    exception_id: Mapped[str | None] = mapped_column(ForeignKey("exceptions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40), default="investigation", nullable=False)

    phase: Mapped[AgentPhase] = mapped_column(
        StrEnumType(AgentPhase, 32), default=AgentPhase.TRIAGE, nullable=False
    )
    outcome: Mapped[AgentOutcome | None] = mapped_column(StrEnumType(AgentOutcome, 32))
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(120))
    policy_version_id: Mapped[str | None] = mapped_column(ForeignKey("policy_versions.id"))

    iterations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    denied_tool_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    invalid_outputs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    recommendation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    cited_evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    rejected_hypotheses: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    abstain_reason: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    steps: Mapped[list[AgentStep]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AgentStep.sequence"
    )


class AgentStep(Base, IdMixin, TimestampMixin):
    __tablename__ = "agent_steps"

    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[AgentPhase] = mapped_column(StrEnumType(AgentPhase, 32), nullable=False)
    thought: Mapped[str | None] = mapped_column(Text)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    run: Mapped[AgentRun] = relationship(back_populates="steps")


class ToolCall(Base, IdMixin, TimestampMixin):
    """Every tool invocation, authorized or denied.

    Denied calls are recorded deliberately: "the agent tried to reach a tool it
    was not permitted to use" is a safety metric, and deleting the attempt
    would erase the evidence.
    """

    __tablename__ = "tool_calls"

    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    step_id: Mapped[str | None] = mapped_column(ForeignKey("agent_steps.id"))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    allowed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    denial_reason: Mapped[str | None] = mapped_column(Text)
    result_summary: Mapped[str | None] = mapped_column(Text)
    result_row_count: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


# ---------------------------------------------------------------------------
# Models, policies, learning
# ---------------------------------------------------------------------------


class ModelVersion(Base, IdMixin, TimestampMixin):
    __tablename__ = "model_versions"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    stage: Mapped[str] = mapped_column(String(24), default="challenger", nullable=False, index=True)
    artifact_path: Mapped[str | None] = mapped_column(String(1000))
    feature_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    hyperparameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    training_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("training_snapshots.id"))
    parent_model_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id"))

    #: Score threshold and conformal risk bound chosen on validation data.
    accept_threshold: Mapped[float | None] = mapped_column(Float)
    review_threshold: Mapped[float | None] = mapped_column(Float)
    risk_budget: Mapped[float | None] = mapped_column(Float)
    conformal_quantile: Mapped[float | None] = mapped_column(Float)

    promoted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    promoted_by: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)


class ModelEvaluation(Base, IdMixin, TimestampMixin):
    __tablename__ = "model_evaluations"

    model_version_id: Mapped[str] = mapped_column(ForeignKey("model_versions.id"), index=True)
    dataset_name: Mapped[str] = mapped_column(String(120), nullable=False)
    split: Mapped[str] = mapped_column(String(24), nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    per_corruption: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    baseline_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    calibration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    passed_gates: Mapped[bool | None] = mapped_column(Boolean)
    gate_failures: Mapped[list[str]] = mapped_column(JSON, default=list)


class TrainingSnapshot(Base, IdMixin, TimestampMixin):
    __tablename__ = "training_snapshots"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    positive_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    human_label_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    artifact_path: Mapped[str | None] = mapped_column(String(1000))
    seed: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PolicyVersion(Base, IdMixin, TimestampMixin):
    __tablename__ = "policy_versions"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------


class AnomalyEvent(Base, IdMixin, TimestampMixin):
    __tablename__ = "anomaly_events"

    batch_id: Mapped[str] = mapped_column(ForeignKey("reconciliation_batches.id"), index=True)
    detector: Mapped[str] = mapped_column(String(60), nullable=False)
    kind: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    subject_record_id: Mapped[str | None] = mapped_column(ForeignKey("source_records.id"))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class DriftReport(Base, IdMixin, TimestampMixin):
    __tablename__ = "drift_reports"

    batch_id: Mapped[str] = mapped_column(ForeignKey("reconciliation_batches.id"), index=True)
    reference_batch_id: Mapped[str | None] = mapped_column(ForeignKey("reconciliation_batches.id"))
    drift_detected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_psi: Mapped[float | None] = mapped_column(Float)
    features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    triggered_restriction: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)


class BatchBriefing(Base, IdMixin, TimestampMixin):
    """Output of the batch-health agent: a controller-facing narrative."""

    __tablename__ = "batch_briefings"

    batch_id: Mapped[str] = mapped_column(ForeignKey("reconciliation_batches.id"), index=True)
    agent_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_actions: Mapped[list[str]] = mapped_column(JSON, default=list)
    cited_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provider: Mapped[str] = mapped_column(String(60), nullable=False)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class AuditEvent(Base, IdMixin, TimestampMixin):
    """Append-only. No code path updates or deletes a row in this table."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_batch_created", "batch_id", "created_at"),)

    batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("reconciliation_batches.id"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[AuditAction] = mapped_column(
        StrEnumType(AuditAction, 48), nullable=False, index=True
    )
    actor: Mapped[Actor] = mapped_column(StrEnumType(Actor, 16), nullable=False)
    actor_detail: Mapped[str | None] = mapped_column(String(200))

    subject_type: Mapped[str | None] = mapped_column(String(60))
    subject_id: Mapped[str | None] = mapped_column(String(64), index=True)
    agent_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))

    model_version_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id"))
    policy_version_id: Mapped[str | None] = mapped_column(ForeignKey("policy_versions.id"))
    input_sha256: Mapped[str | None] = mapped_column(String(64))

    previous_state: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    resulting_state: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    message: Mapped[str | None] = mapped_column(Text)
