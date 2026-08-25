"""Response models.

Amounts cross the wire as integer subunits *and* a formatted string. The integer
is what clients should compute with; the string is what they should display.
Sending a float would reintroduce the rounding error the whole domain layer
exists to avoid.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from reconproof.domain.money import Money


class Amount(BaseModel):
    subunits: int
    currency: str
    formatted: str

    @classmethod
    def of(cls, subunits: int | None, currency: str = "INR") -> Amount:
        money = Money(subunits or 0, currency)
        return cls(subunits=money.subunits, currency=money.currency, formatted=money.format())


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------


class BatchSummary(ApiModel):
    id: str
    name: str
    status: str
    currency: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    record_count: int = 0
    exception_count: int = 0
    open_exception_count: int = 0
    unresolved: Amount | None = None
    automation_restricted: bool = False
    restriction_reason: str | None = None
    dataset_seed: int | None = None


class SourceFileSummary(ApiModel):
    id: str
    source_kind: str
    filename: str
    content_sha256: str
    byte_size: int
    row_count: int
    accepted_rows: int
    rejected_rows: int
    detected_columns: dict[str, str] = Field(default_factory=dict)
    validation_errors: list[dict[str, Any]] = Field(default_factory=list)


class BatchMetrics(BaseModel):
    total_records: int
    records_by_kind: dict[str, int]
    exact_links: int
    candidates_generated: int
    auto_accepted: int
    sent_to_review: int
    rejected_by_invariant: int
    displaced_by_assignment: int
    forced_review_by_tie: int
    balanced_settlements: int
    unbalanced_settlements: int
    automatic_match_rate: float
    money_weighted_rate: float
    settlement_value: Amount
    settlement_value_traced: Amount
    unexplained: Amount
    exception_total: Amount
    unresolved_value_fully_represented: bool
    exceptions_by_category: dict[str, int]
    scorer: str
    accept_threshold: float | None = None
    risk_budget: float | None = None
    duration_ms: int


class BatchDetail(BatchSummary):
    sources: list[SourceFileSummary] = Field(default_factory=list)
    metrics: BatchMetrics | None = None
    policy_version: str | None = None
    model_version: str | None = None


# ---------------------------------------------------------------------------
# Records, matches, evidence
# ---------------------------------------------------------------------------


class RecordSummary(ApiModel):
    id: str
    source_kind: str
    record_kind: str
    external_id: str | None = None
    reference: str | None = None
    amount: Amount
    occurred_at: datetime | None = None
    timestamp_is_date_only: bool = False
    description: str | None = None
    counterparty: str | None = None
    payment_method: str
    status: str
    raw: dict[str, Any] = Field(default_factory=dict)


class EvidenceSummary(ApiModel):
    id: str
    kind: str
    statement: str
    supports: bool
    weight: float
    produced_by: str
    detail: dict[str, Any] = Field(default_factory=dict)


class InvariantSummary(BaseModel):
    invariant: str
    passed: bool
    message: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class MatchSummary(ApiModel):
    id: str
    relation: str
    decision: str
    method: str
    score: float | None = None
    risk: float | None = None
    allocated: Amount
    rationale: str | None = None
    invariants_passed: list[str] = Field(default_factory=list)
    invariants_failed: list[str] = Field(default_factory=list)
    left: RecordSummary
    right: RecordSummary


class FeatureContribution(BaseModel):
    """One feature's signed push on this specific candidate's score.

    Distinct from the model page's global coefficients: this is the
    coefficient times *this candidate's* standardized value, so it answers
    "what mattered for this one link" rather than "what the model weighs in
    general."
    """

    feature: str
    raw_value: float
    standardized_value: float
    coefficient: float
    contribution: float


class CandidateSummary(BaseModel):
    id: str
    relation: str
    generator: str
    score: float | None
    risk: float | None
    features: dict[str, float]
    feature_contributions: list[FeatureContribution] = Field(default_factory=list)
    left: RecordSummary
    right: RecordSummary
    evidence: list[EvidenceSummary] = Field(default_factory=list)
    invariants: list[InvariantSummary] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CounterfactualSummary(BaseModel):
    """What would have to change for this link to be acceptable."""

    statements: list[str] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)


class ExceptionSummary(ApiModel):
    id: str
    batch_id: str
    category: str
    status: str
    amount: Amount
    summary: str
    best_score: float | None = None
    best_risk: float | None = None
    blocking_invariants: list[str] = Field(default_factory=list)
    explanation: str | None = None
    explanation_provider: str | None = None
    created_at: datetime
    subject: RecordSummary
    agent_run_id: str | None = None
    agent_outcome: str | None = None


class ExceptionDetail(ExceptionSummary):
    candidates: list[CandidateSummary] = Field(default_factory=list)
    evidence: list[EvidenceSummary] = Field(default_factory=list)
    counterfactual: CounterfactualSummary | None = None
    resolution: ResolutionSummary | None = None


class ResolutionSummary(ApiModel):
    id: str
    reviewer: str
    action: str
    notes: str | None = None
    chosen_candidate_id: str | None = None
    overrode_agent: bool = False
    created_at: datetime


class ResolveRequest(BaseModel):
    action: str = Field(pattern="^(approve|choose|mark_unresolved|write_off)$")
    reviewer: str = Field(default="reviewer", min_length=1, max_length=120)
    candidate_id: str | None = None
    notes: str | None = Field(default=None, max_length=4000)
    reclassify: str | None = None


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class ToolCallSummary(ApiModel):
    id: str
    sequence: int
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    allowed: bool
    denial_reason: str | None = None
    result_summary: str | None = None
    result_row_count: int | None = None
    error: str | None = None
    duration_ms: int | None = None


class AgentStepSummary(ApiModel):
    id: str
    sequence: int
    phase: str
    thought: str | None = None
    output: dict[str, Any] | None = None
    duration_ms: int | None = None


class AgentRunSummary(ApiModel):
    id: str
    batch_id: str
    exception_id: str | None = None
    kind: str
    phase: str
    outcome: str | None = None
    provider: str
    model_name: str | None = None
    iterations: int
    tool_calls: int
    denied_tool_calls: int
    invalid_outputs: int
    duration_ms: int | None = None
    recommendation: dict[str, Any] | None = None
    cited_evidence_ids: list[str] = Field(default_factory=list)
    rejected_hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    abstain_reason: str | None = None
    created_at: datetime


class AgentRunDetail(AgentRunSummary):
    steps: list[AgentStepSummary] = Field(default_factory=list)
    tools: list[ToolCallSummary] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Graph, audit, monitoring
# ---------------------------------------------------------------------------


class GraphNode(BaseModel):
    id: str
    kind: str
    source: str
    amount: Amount
    reference: str | None = None
    occurred_at: str | None = None


class GraphEdge(BaseModel):
    source: str
    target: str
    relation: str
    decision: str
    method: str
    score: float | None = None
    risk: float | None = None
    allocated: Amount
    blocking: list[str] = Field(default_factory=list)


class GraphResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool = False
    total_edges: int = 0


class AuditEntry(ApiModel):
    id: str
    sequence: int
    action: str
    actor: str
    actor_detail: str | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    message: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    input_sha256: str | None = None
    created_at: datetime


class AnomalySummary(ApiModel):
    id: str
    detector: str
    kind: str
    severity: str
    score: float | None = None
    summary: str
    detail: dict[str, Any] = Field(default_factory=dict)


class DriftSummary(ApiModel):
    id: str
    drift_detected: bool
    max_psi: float | None = None
    triggered_restriction: bool
    summary: str | None = None
    features: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class BriefingSummary(ApiModel):
    id: str
    headline: str
    body: str
    recommended_actions: list[str] = Field(default_factory=list)
    cited_metrics: dict[str, Any] = Field(default_factory=dict)
    provider: str
    created_at: datetime


class ModelVersionSummary(ApiModel):
    id: str
    name: str
    kind: str
    stage: str
    accept_threshold: float | None = None
    risk_budget: float | None = None
    feature_names: list[str] = Field(default_factory=list)
    promoted_at: datetime | None = None
    promoted_by: str | None = None
    notes: str | None = None
    created_at: datetime
    #: Latest recorded evaluation, attached by the list endpoint so a reviewer
    #: can see whether promotion is even eligible without a second round-trip.
    gates_passed: bool | None = None
    gate_failures: list[str] = Field(default_factory=list)
    precision_lower_bound: float | None = None
    coverage: float | None = None


class PolicyVersionSummary(ApiModel):
    id: str
    name: str
    version: str
    document_sha256: str
    active: bool
    notes: str | None = None
    created_at: datetime
    #: The full document, not just the fields `/api/config` curates for the
    #: active policy. A reviewer or auditor comparing two versions needs the
    #: agent tool allowlist, review rules and drift settings, not only the
    #: automation threshold.
    document: dict[str, Any] = Field(default_factory=dict)


class RazorpaySyncRequest(BaseModel):
    batch_id: str
    from_time: datetime
    to_time: datetime


class RazorpaySyncResult(ApiModel):
    batch_id: str
    payments: SourceFileSummary | None = None
    refunds: SourceFileSummary | None = None


class WebhookIngestResult(ApiModel):
    batch_id: str
    event: str
    accepted: bool
    duplicate: bool
    source: SourceFileSummary | None = None


class HealthResponse(BaseModel):
    status: str
    database: str
    llm_provider: str
    llm_available: bool
    model_stage: str | None = None
    semantic_matching: bool
    version: str


ExceptionDetail.model_rebuild()
