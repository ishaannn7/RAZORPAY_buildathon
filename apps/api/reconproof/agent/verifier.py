"""Deterministic verification of agent output.

This is the boundary that makes a language model safe to use here. The agent may
propose anything; nothing it proposes takes effect until it passes every check
below. The checks are ordered so that the cheapest and most damning run first.

The most important one is evidence existence. A model that cites
``evidence_9f3a`` when no such row exists has not made a small formatting error —
it has produced a justification that cannot be traced, which is exactly the
failure that makes AI unusable in finance. Rather than trusting the citation, the
verifier looks it up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from reconproof.accounting import invariants as inv
from reconproof.agent.providers.base import Hypothesis
from reconproof.db.models import EvidenceItem, MatchCandidate, ReconciliationException, SourceRecord
from reconproof.domain.entities import MatchRelation
from reconproof.policy.engine import AgentBudget


@dataclass(slots=True)
class VerificationResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    verified_evidence_ids: list[str] = field(default_factory=list)
    hallucinated_evidence_ids: list[str] = field(default_factory=list)
    invariant_names_passed: list[str] = field(default_factory=list)
    invariant_names_failed: list[str] = field(default_factory=list)
    allocated_subunits: int | None = None

    @property
    def cited_hallucination(self) -> bool:
        return bool(self.hallucinated_evidence_ids)


def verify_hypothesis(
    session: Session,
    *,
    exception: ReconciliationException,
    hypothesis: Hypothesis,
    budget: AgentBudget,
) -> VerificationResult:
    """Check an agent proposal against the database and the invariants."""
    result = VerificationResult(passed=False)

    # 1. Every cited evidence id must exist and belong to this batch.
    cited = list(dict.fromkeys(hypothesis.cited_evidence_ids))
    if cited:
        found = {
            item.id
            for item in session.execute(
                select(EvidenceItem).where(
                    EvidenceItem.id.in_(cited),
                    EvidenceItem.batch_id == exception.batch_id,
                )
            ).scalars()
        }
    else:
        found = set()
    result.verified_evidence_ids = [item for item in cited if item in found]
    result.hallucinated_evidence_ids = [item for item in cited if item not in found]
    if result.hallucinated_evidence_ids:
        result.failures.append(
            f"{len(result.hallucinated_evidence_ids)} cited evidence id(s) do not exist "
            f"in this batch: {', '.join(result.hallucinated_evidence_ids[:5])}"
        )

    # 2. A proposal of "no link" needs no further structural checks.
    if hypothesis.candidate_id is None:
        result.passed = not result.failures
        if result.passed:
            result.warnings.append("Proposes that no link should be made.")
        return result

    # 3. The candidate must exist in this batch.
    candidate = session.get(MatchCandidate, hypothesis.candidate_id)
    if candidate is None or candidate.batch_id != exception.batch_id:
        result.failures.append(f"candidate {hypothesis.candidate_id} does not exist in this batch")
        return result

    # 4. The candidate must actually involve the exception's subject, otherwise
    #    the agent has resolved a different case than the one it was given.
    if exception.subject_record_id not in {
        candidate.left_record_id,
        candidate.right_record_id,
    }:
        result.failures.append(
            "the proposed candidate does not involve this exception's subject record"
        )

    left = session.get(SourceRecord, candidate.left_record_id)
    right = session.get(SourceRecord, candidate.right_record_id)
    if left is None or right is None:  # pragma: no cover - foreign keys
        result.failures.append("candidate records are missing")
        return result

    # 5. Recompute the allocation. The agent's own arithmetic is never used.
    if candidate.relation in {
        MatchRelation.REFUND_TO_SETTLEMENT,
        MatchRelation.FEE_TO_SETTLEMENT,
    }:
        allocated = -abs(left.amount_subunits)
        if candidate.relation is MatchRelation.FEE_TO_SETTLEMENT:
            allocated -= abs(left.tax_subunits or 0)
    else:
        allocated = left.amount_subunits
    result.allocated_subunits = allocated

    # 6. Every accounting invariant must hold.
    proposal = inv.AllocationProposal(
        left=left, right=right, relation=candidate.relation, allocated_subunits=allocated
    )
    checks = inv.evaluate_pairwise(proposal, inv.LedgerView())
    result.invariant_names_passed = [check.name for check in checks if check.passed]
    result.invariant_names_failed = [check.name for check in inv.blocking_failures(checks)]
    if result.invariant_names_failed:
        result.failures.append(
            "the proposed link fails accounting invariant(s): "
            + ", ".join(result.invariant_names_failed)
        )
    advisory = [check.name for check in inv.advisory_failures(checks)]
    if advisory:
        result.warnings.append("advisory check(s) failed: " + ", ".join(advisory))

    # 7. The policy's evidence floor must be met by *verified* citations only.
    if len(result.verified_evidence_ids) < budget.min_cited_evidence:
        result.failures.append(
            f"only {len(result.verified_evidence_ids)} verified evidence citation(s); "
            f"the policy requires {budget.min_cited_evidence}"
        )

    result.passed = not result.failures
    return result
