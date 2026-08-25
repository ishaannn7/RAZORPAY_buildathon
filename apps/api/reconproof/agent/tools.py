"""Tools available to the investigation agent.

Every tool is read-only, typed, authorized against the active policy, row-capped
and audited. There is deliberately no tool that executes SQL, writes a record,
posts a match, changes a threshold or reads the filesystem: the agent's entire
reachable surface is this module, so its authority is bounded by what is written
here rather than by what a prompt asks it to do.

Each tool returns evidence rows that already exist in the database. That is what
makes a citation checkable: the verifier can confirm every id the agent cites,
and a fabricated one fails rather than persuades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from reconproof.accounting import invariants as inv
from reconproof.db.models import (
    AccountingCheck,
    EvidenceItem,
    MatchCandidate,
    ReconciliationException,
    SourceRecord,
)
from reconproof.domain.entities import MatchRelation, RecordKind
from reconproof.domain.money import Money
from reconproof.policy.engine import AgentBudget, PolicyEngine


class ToolDenied(Exception):
    """Raised when the agent calls a tool the policy does not permit."""


class ToolInputError(Exception):
    """Raised when tool arguments fail validation."""


@dataclass(slots=True)
class ToolResult:
    tool: str
    summary: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def row_count(self) -> int:
        return len(self.rows)


def _record_view(record: SourceRecord) -> dict[str, Any]:
    """The projection the agent is allowed to see.

    Ground-truth columns are excluded. Exposing ``truth_group`` would let the
    agent read the answer key, and any evaluation of its reasoning would then be
    measuring nothing.
    """
    return {
        "record_id": record.id,
        "kind": record.record_kind.value,
        "source": record.source_kind.value,
        "amount": Money(record.amount_subunits, record.currency).format(),
        "amount_subunits": record.amount_subunits,
        "currency": record.currency,
        "occurred_at": record.occurred_at.isoformat() if record.occurred_at else None,
        "date_only": record.timestamp_is_date_only,
        "reference": (
            record.settlement_ref
            or record.payment_ref
            or record.order_ref
            or record.bank_ref
            or record.external_id
        ),
        "description": _redact(record.description),
        "fee": record.fee_subunits,
        "tax": record.tax_subunits,
        "refund_total": record.refund_total_subunits,
    }


#: Substrings that mark an instruction-shaped payload embedded in source text.
#: Bank narrations are untrusted input: they are merchant- and customer-supplied
#: strings that reach the model verbatim. Neutralizing them here keeps a crafted
#: description from being read as guidance.
_INJECTION_MARKERS = (
    "ignore previous",
    "ignore all previous",
    "system:",
    "assistant:",
    "</evidence>",
    "<|",
    "note to reviewer agent",
    "auto-approve",
    "approve this match",
    "mark as reconciled",
    "disregard",
)


def _redact(text: str | None) -> str | None:
    """Neutralize instruction-shaped content in untrusted free text.

    The value is preserved so a reviewer can still read what the source said;
    only its ability to read as an instruction is removed. The agent is also
    told the field was flagged, which is more useful than silently deleting it.
    """
    if not text:
        return text
    lowered = text.lower()
    if any(marker in lowered for marker in _INJECTION_MARKERS):
        return (
            "[flagged: this source field contains instruction-like text and has been "
            f"quoted inertly] {text!r}"
        )
    return text


class InvestigationTools:
    """The bounded tool surface for one investigation."""

    def __init__(
        self,
        session: Session,
        *,
        exception: ReconciliationException,
        policy: PolicyEngine,
        budget: AgentBudget,
    ) -> None:
        self.session = session
        self.exception = exception
        self.policy = policy
        self.budget = budget
        self.calls: list[tuple[str, dict[str, Any], bool, str | None]] = []
        self._call_count = 0

    # -- dispatch ----------------------------------------------------------

    @property
    def available(self) -> list[str]:
        return sorted(self.budget.allowed_tools)

    def call(self, tool_name: str, **arguments: Any) -> ToolResult:
        """Invoke a tool by name, enforcing the policy allowlist and budget.

        A denied call is recorded rather than silently dropped: "the agent tried
        to reach a tool it was not permitted to use" is a safety signal, and
        discarding the attempt would erase the evidence of it.
        """
        if tool_name not in self.budget.allowed_tools:
            self.calls.append((tool_name, arguments, False, "not in policy allowlist"))
            raise ToolDenied(f"tool {tool_name!r} is not permitted by the active policy")
        handler = getattr(self, f"tool_{tool_name}", None)
        if handler is None:
            self.calls.append((tool_name, arguments, False, "no such tool"))
            raise ToolDenied(f"tool {tool_name!r} does not exist")
        if self._call_count >= self.budget.max_tool_calls:
            self.calls.append((tool_name, arguments, False, "tool call budget exhausted"))
            raise ToolDenied("tool call budget exhausted")

        self._call_count += 1
        try:
            result = handler(**arguments)
        except TypeError as exc:
            self.calls.append((tool_name, arguments, False, f"invalid arguments: {exc}"))
            raise ToolInputError(f"invalid arguments for {tool_name}: {exc}") from exc
        self.calls.append((tool_name, arguments, True, None))
        return result

    # -- tools -------------------------------------------------------------

    def tool_get_record_evidence(self, record_id: str) -> ToolResult:
        """Everything known about one record, plus the evidence citing it."""
        record = self.session.get(SourceRecord, record_id)
        if record is None or record.batch_id != self.exception.batch_id:
            raise ToolInputError(f"record {record_id} is not part of this batch")
        evidence = list(
            self.session.execute(
                select(EvidenceItem)
                .where(
                    EvidenceItem.batch_id == self.exception.batch_id,
                    (EvidenceItem.subject_record_id == record_id)
                    | (EvidenceItem.related_record_id == record_id),
                )
                .limit(self.budget.max_rows_per_search)
            ).scalars()
        )
        return ToolResult(
            tool="get_record_evidence",
            summary=f"Record {record_id} with {len(evidence)} evidence item(s).",
            rows=[_record_view(record)],
            evidence_ids=[item.id for item in evidence],
            detail={
                "evidence": [
                    {
                        "evidence_id": item.id,
                        "kind": item.kind,
                        "statement": _redact(item.statement),
                        "supports": item.supports,
                    }
                    for item in evidence
                ]
            },
        )

    def tool_search_source_records(
        self,
        record_kind: str | None = None,
        amount_subunits: int | None = None,
        amount_tolerance_subunits: int = 0,
        reference_contains: str | None = None,
        days_around: int | None = None,
        anchor_record_id: str | None = None,
    ) -> ToolResult:
        """Search the batch's records under an explicit row cap."""
        statement = select(SourceRecord).where(SourceRecord.batch_id == self.exception.batch_id)
        if record_kind:
            try:
                statement = statement.where(SourceRecord.record_kind == RecordKind(record_kind))
            except ValueError as exc:
                raise ToolInputError(f"unknown record kind {record_kind!r}") from exc
        if amount_subunits is not None:
            low = amount_subunits - max(0, amount_tolerance_subunits)
            high = amount_subunits + max(0, amount_tolerance_subunits)
            statement = statement.where(SourceRecord.amount_subunits.between(low, high))
        if reference_contains:
            needle = f"%{reference_contains.strip().lower()}%"
            statement = statement.where(
                SourceRecord.bank_ref_normalized.ilike(needle)
                | SourceRecord.description_normalized.ilike(needle)
            )
        if days_around is not None and anchor_record_id:
            anchor = self.session.get(SourceRecord, anchor_record_id)
            if anchor is not None and anchor.occurred_at is not None:
                window = timedelta(days=max(0, days_around))
                statement = statement.where(
                    SourceRecord.occurred_at.between(
                        anchor.occurred_at - window, anchor.occurred_at + window
                    )
                )

        # The cap is a policy value, not a performance tweak: an unbounded read
        # would let the agent pull the whole batch into its context and reason
        # over data no one reviewed.
        rows = list(
            self.session.execute(statement.limit(self.budget.max_rows_per_search)).scalars()
        )
        return ToolResult(
            tool="search_source_records",
            summary=f"Found {len(rows)} record(s) matching the search.",
            rows=[_record_view(record) for record in rows],
            detail={"capped_at": self.budget.max_rows_per_search},
        )

    def tool_find_match_candidates(self, record_id: str) -> ToolResult:
        """Candidates the pipeline already scored for a record."""
        candidates = list(
            self.session.execute(
                select(MatchCandidate)
                .where(
                    MatchCandidate.batch_id == self.exception.batch_id,
                    (MatchCandidate.left_record_id == record_id)
                    | (MatchCandidate.right_record_id == record_id),
                )
                .order_by(MatchCandidate.score.desc().nullslast())
                .limit(self.budget.max_rows_per_search)
            ).scalars()
        )
        rows = []
        for candidate in candidates:
            left = self.session.get(SourceRecord, candidate.left_record_id)
            right = self.session.get(SourceRecord, candidate.right_record_id)
            if left is None or right is None:
                continue
            rows.append(
                {
                    "candidate_id": candidate.id,
                    "relation": candidate.relation.value,
                    "score": candidate.score,
                    "risk": candidate.risk,
                    "generator": candidate.generator,
                    "left": _record_view(left),
                    "right": _record_view(right),
                    "features": candidate.features,
                }
            )
        return ToolResult(
            tool="find_match_candidates",
            summary=f"{len(rows)} scored candidate(s) involve this record.",
            rows=rows,
        )

    def tool_calculate_allocation(self, candidate_id: str) -> ToolResult:
        """Compute the allocation a candidate implies. Deterministic arithmetic.

        The agent must call this rather than compute amounts itself. A model
        doing ledger arithmetic in prose is a source of plausible wrong numbers,
        and there is no reason to accept that risk when the calculation is exact.
        """
        candidate = self._require_candidate(candidate_id)
        left = self.session.get(SourceRecord, candidate.left_record_id)
        right = self.session.get(SourceRecord, candidate.right_record_id)
        if left is None or right is None:
            raise ToolInputError("candidate records are missing")

        if candidate.relation in {
            MatchRelation.REFUND_TO_SETTLEMENT,
            MatchRelation.FEE_TO_SETTLEMENT,
        }:
            allocated = -abs(left.amount_subunits) - (
                abs(left.tax_subunits or 0)
                if candidate.relation is MatchRelation.FEE_TO_SETTLEMENT
                else 0
            )
        else:
            allocated = left.amount_subunits

        return ToolResult(
            tool="calculate_allocation",
            summary=(
                f"Allocating {Money(allocated, left.currency)} from {left.record_kind.value} "
                f"to {right.record_kind.value}."
            ),
            rows=[
                {
                    "candidate_id": candidate.id,
                    "relation": candidate.relation.value,
                    "allocated_subunits": allocated,
                    "allocated": Money(allocated, left.currency).format(),
                    "currency": left.currency,
                }
            ],
        )

    def tool_check_accounting_invariants(self, candidate_id: str) -> ToolResult:
        """Run the invariants against a candidate and return the verdicts."""
        candidate = self._require_candidate(candidate_id)
        left = self.session.get(SourceRecord, candidate.left_record_id)
        right = self.session.get(SourceRecord, candidate.right_record_id)
        if left is None or right is None:
            raise ToolInputError("candidate records are missing")

        allocation = self.tool_calculate_allocation(candidate_id).rows[0]
        proposal = inv.AllocationProposal(
            left=left,
            right=right,
            relation=candidate.relation,
            allocated_subunits=int(allocation["allocated_subunits"]),
        )
        results = inv.evaluate_pairwise(proposal, inv.LedgerView())
        blocking = [result.name for result in inv.blocking_failures(results)]
        return ToolResult(
            tool="check_accounting_invariants",
            summary=(
                "All invariants passed."
                if not blocking
                else f"{len(blocking)} invariant(s) failed: {', '.join(blocking)}"
            ),
            rows=[
                {
                    "invariant": result.name,
                    "passed": result.passed,
                    "advisory": result.advisory,
                    "message": result.message,
                }
                for result in results
            ],
            detail={"blocking": blocking},
        )

    def tool_compare_fee_and_tax_breakdown(self, candidate_id: str) -> ToolResult:
        """Compare a payment's reported fee against the standard schedule."""
        candidate = self._require_candidate(candidate_id)
        left = self.session.get(SourceRecord, candidate.left_record_id)
        if left is None:
            raise ToolInputError("candidate records are missing")
        gross = abs(left.amount_subunits)
        reported = abs(left.fee_subunits or 0) + abs(left.tax_subunits or 0)
        expected_fee = Money(gross, left.currency).apply_rate("0.02")
        expected_total = expected_fee + expected_fee.apply_rate("0.18")
        return ToolResult(
            tool="compare_fee_and_tax_breakdown",
            summary=(
                f"Reported fee and tax {Money(reported, left.currency)} against an expected "
                f"{expected_total} at the standard 2% plus 18% GST."
            ),
            rows=[
                {
                    "record_id": left.id,
                    "gross_subunits": gross,
                    "reported_fee_tax_subunits": reported,
                    "expected_fee_tax_subunits": expected_total.subunits,
                    "difference_subunits": reported - expected_total.subunits,
                }
            ],
        )

    def tool_get_related_refunds(self, record_id: str) -> ToolResult:
        """Refunds referencing a payment."""
        record = self.session.get(SourceRecord, record_id)
        if record is None:
            raise ToolInputError(f"record {record_id} not found")
        reference = record.payment_ref or record.external_id
        rows: list[dict[str, Any]] = []
        if reference:
            refunds = list(
                self.session.execute(
                    select(SourceRecord)
                    .where(
                        SourceRecord.batch_id == self.exception.batch_id,
                        SourceRecord.record_kind == RecordKind.REFUND,
                        SourceRecord.payment_ref == reference,
                    )
                    .limit(self.budget.max_rows_per_search)
                ).scalars()
            )
            rows = [_record_view(refund) for refund in refunds]
        return ToolResult(
            tool="get_related_refunds",
            summary=f"{len(rows)} refund(s) reference this payment.",
            rows=rows,
        )

    def tool_inspect_duplicate_events(self, record_id: str) -> ToolResult:
        """Look for other records that could be duplicates of this one."""
        record = self.session.get(SourceRecord, record_id)
        if record is None:
            raise ToolInputError(f"record {record_id} not found")
        siblings = list(
            self.session.execute(
                select(SourceRecord)
                .where(
                    SourceRecord.batch_id == self.exception.batch_id,
                    SourceRecord.record_kind == record.record_kind,
                    SourceRecord.amount_subunits == record.amount_subunits,
                    SourceRecord.id != record.id,
                )
                .limit(self.budget.max_rows_per_search)
            ).scalars()
        )
        same_reference = [
            sibling
            for sibling in siblings
            if sibling.bank_ref_normalized
            and sibling.bank_ref_normalized == record.bank_ref_normalized
        ]
        return ToolResult(
            tool="inspect_duplicate_events",
            summary=(
                f"{len(siblings)} record(s) share this amount; {len(same_reference)} also "
                "share the reference, which would indicate a true duplicate."
            ),
            rows=[_record_view(sibling) for sibling in siblings],
            detail={"same_reference_ids": [sibling.id for sibling in same_reference]},
        )

    def tool_retrieve_reconciliation_policy(self, section: str | None = None) -> ToolResult:
        """Read the active policy. Read-only: the agent cannot change it."""
        document = self.policy.document
        payload = (
            document.get(section, {})
            if section
            else {
                "automation": document.get("automation", {}),
                "review": document.get("review", {}),
                "agent": {
                    "min_cited_evidence": self.budget.min_cited_evidence,
                    "recommendation_requires_human_approval": self.budget.requires_human_approval,
                },
            }
        )
        return ToolResult(
            tool="retrieve_reconciliation_policy",
            summary=f"Policy {self.policy.name}@{self.policy.version}.",
            rows=[{"section": section or "summary", "content": payload}],
        )

    # -- helpers -----------------------------------------------------------

    def _require_candidate(self, candidate_id: str) -> MatchCandidate:
        candidate = self.session.get(MatchCandidate, candidate_id)
        if candidate is None or candidate.batch_id != self.exception.batch_id:
            raise ToolInputError(f"candidate {candidate_id} is not part of this batch")
        return candidate

    def accounting_checks_for(self, candidate_id: str) -> list[AccountingCheck]:
        return list(
            self.session.execute(
                select(AccountingCheck).where(AccountingCheck.candidate_id == candidate_id)
            ).scalars()
        )


#: Machine-readable tool descriptions, used to build the prompt for an LLM
#: provider and to document the surface for reviewers.
TOOL_SPECS: dict[str, dict[str, Any]] = {
    "search_source_records": {
        "description": (
            "Search this batch's records by kind, amount, reference text or date window."
        ),
        "arguments": {
            "record_kind": "one of order, payment, refund, settlement, bank_credit, fee",
            "amount_subunits": "integer minor units to match",
            "amount_tolerance_subunits": "integer slack around the amount",
            "reference_contains": "substring to look for in references or narration",
            "days_around": "date window size in days, requires anchor_record_id",
            "anchor_record_id": "record whose date anchors the window",
        },
    },
    "get_record_evidence": {
        "description": "Fetch one record and the evidence items citing it.",
        "arguments": {"record_id": "record identifier"},
    },
    "find_match_candidates": {
        "description": "List already-scored candidate links involving a record.",
        "arguments": {"record_id": "record identifier"},
    },
    "calculate_allocation": {
        "description": (
            "Compute exactly what amount a candidate would allocate. Use this instead "
            "of doing arithmetic yourself."
        ),
        "arguments": {"candidate_id": "candidate identifier"},
    },
    "check_accounting_invariants": {
        "description": (
            "Run the accounting invariants against a candidate and return each verdict."
        ),
        "arguments": {"candidate_id": "candidate identifier"},
    },
    "compare_fee_and_tax_breakdown": {
        "description": ("Compare a payment's reported fee and tax against the standard schedule."),
        "arguments": {"candidate_id": "candidate identifier"},
    },
    "get_related_refunds": {
        "description": "Find refunds that reference a payment.",
        "arguments": {"record_id": "record identifier"},
    },
    "inspect_duplicate_events": {
        "description": "Look for records that may be duplicates of this one.",
        "arguments": {"record_id": "record identifier"},
    },
    "retrieve_reconciliation_policy": {
        "description": "Read the active reconciliation policy.",
        "arguments": {"section": "optional policy section name"},
    },
}
