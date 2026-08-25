"""Policy engine.

The model decides how *likely* a link is. Policy decides whether that is enough
to act without a human. Keeping the two separate is what makes the automation
boundary auditable: a decision can be re-derived from a policy version and a
score, and neither the model nor the agent can edit the policy that governs it.

Policies are plain data so they can be versioned, hashed and diffed. The rules
are evaluated in a fixed order and every rule that fired is recorded, so the
answer to "why did this need approval" is a list rather than an inference.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reconproof.domain.entities import MatchDecision
from reconproof.matching.types import ProposedLink

DEFAULT_POLICY: dict[str, Any] = {
    "name": "reconproof-default",
    "version": "1.0.0",
    "automation": {
        # A link is auto-accepted only when its estimated error probability is
        # at or below this. Score alone is never sufficient.
        "max_risk": 0.01,
        "min_score": 0.90,
        # Above this value a *statistically* matched link needs sign-off. Large
        # amounts concentrate the cost of a mistake, and a probabilistic match is
        # where that cost is actually at risk.
        "high_value_review_subunits": 50_000_00,
        # Identifier matches get a much higher bar. A settlement whose UTR
        # appears verbatim in the bank narration is proven, not estimated, so
        # routing every large settlement to a human would spend review capacity
        # on the cases that need it least. The threshold is not infinite: at
        # some size a typo in a source file is worth a second pair of eyes.
        "high_value_review_subunits_identifier_match": 25_00_000_00,
        # Relations whose evidence is definitional rather than statistical may
        # be accepted on an exact identifier alone.
        "trusted_methods": ["exact_reference", "exact_composite"],
        "require_evidence": True,
        "min_supporting_evidence": 1,
    },
    "review": {
        "force_on_tie": True,
        "force_on_advisory_failure": True,
        "force_when_competing_candidates_above": 3,
    },
    "agent": {
        "max_iterations": 8,
        "max_tool_calls": 24,
        "max_output_retries": 1,
        "allowed_tools": [
            "search_source_records",
            "get_record_evidence",
            "find_match_candidates",
            "calculate_allocation",
            "check_accounting_invariants",
            "compare_fee_and_tax_breakdown",
            "get_related_refunds",
            "inspect_duplicate_events",
            "retrieve_reconciliation_policy",
            "submit_recommendation",
            "abstain",
        ],
        # An agent recommendation never posts to the ledger. It becomes a
        # proposal for a human, and only a human can accept it.
        "recommendation_requires_human_approval": True,
        "min_cited_evidence": 2,
        "max_rows_per_search": 50,
    },
    "model_promotion": {
        "min_precision_lower_bound": 0.99,
        "require_no_regression_in_precision": True,
        "min_coverage_improvement": 0.0,
        "requires_human_approval": True,
    },
    "drift": {
        "psi_threshold": 0.2,
        "on_detection": "tighten_automation",
        "risk_tightening_factor": 0.5,
    },
}


@dataclass(slots=True)
class PolicyDecision:
    decision: MatchDecision
    reason: str
    rules_fired: list[str] = field(default_factory=list)

    @property
    def automated(self) -> bool:
        return self.decision is MatchDecision.AUTO_ACCEPTED


@dataclass(slots=True)
class AgentBudget:
    max_iterations: int
    max_tool_calls: int
    max_output_retries: int
    allowed_tools: frozenset[str]
    min_cited_evidence: int
    max_rows_per_search: int
    requires_human_approval: bool


class PolicyEngine:
    """Evaluates the active policy document against a proposed action."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self._risk_tightening = 1.0

    @classmethod
    def default(cls) -> PolicyEngine:
        return cls(json.loads(json.dumps(DEFAULT_POLICY)))

    @classmethod
    def from_file(cls, path: Path) -> PolicyEngine:
        return cls(json.loads(path.read_text(encoding="utf-8")))

    @property
    def name(self) -> str:
        return str(self.document.get("name", "unnamed"))

    @property
    def version(self) -> str:
        return str(self.document.get("version", "0"))

    def digest(self) -> str:
        encoded = json.dumps(self.document, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    # -- drift response ----------------------------------------------------

    def tighten_for_drift(self, factor: float | None = None) -> None:
        """Reduce the effective risk budget after drift is detected.

        Tightening rather than loosening is the only safe direction: when the
        input distribution has moved, the calibration evidence behind the
        threshold is less applicable, so less should be automated.
        """
        configured = float(self.document.get("drift", {}).get("risk_tightening_factor", 0.5))
        self._risk_tightening = factor if factor is not None else configured

    @property
    def effective_max_risk(self) -> float:
        base = float(self.document["automation"]["max_risk"])
        return base * self._risk_tightening

    # -- match decisions ---------------------------------------------------

    def evaluate_match(self, link: ProposedLink, *, forced_review: bool = False) -> PolicyDecision:
        automation = self.document["automation"]
        review = self.document["review"]
        fired: list[str] = []

        if link.blocking_invariants:
            return PolicyDecision(
                decision=MatchDecision.REJECTED,
                reason=(
                    "Blocked by accounting invariant(s): " + ", ".join(link.blocking_invariants)
                ),
                rules_fired=["blocking_invariant"],
            )

        trusted = set(automation.get("trusted_methods", []))
        if link.method.value in trusted:
            fired.append("trusted_method")
            if link.advisory_invariants and review.get("force_on_advisory_failure", True):
                return PolicyDecision(
                    decision=MatchDecision.HUMAN_REVIEW,
                    reason=(
                        "Identifier match, but an advisory check failed: "
                        + ", ".join(link.advisory_invariants)
                    ),
                    rules_fired=[*fired, "advisory_failure"],
                )
            identifier_limit = int(
                automation.get(
                    "high_value_review_subunits_identifier_match",
                    automation.get("high_value_review_subunits", 0),
                )
            )
            if abs(link.allocated_subunits) > identifier_limit:
                return PolicyDecision(
                    decision=MatchDecision.HUMAN_REVIEW,
                    reason=(
                        "Identifier match, but the amount exceeds the sign-off threshold "
                        "for automatic posting."
                    ),
                    rules_fired=[*fired, "high_value_identifier"],
                )
            return PolicyDecision(
                decision=MatchDecision.AUTO_ACCEPTED,
                reason="Both records carry the same identifier.",
                rules_fired=fired,
            )

        if forced_review and review.get("force_on_tie", True):
            return PolicyDecision(
                decision=MatchDecision.HUMAN_REVIEW,
                reason=(
                    "Another candidate scored within the tie margin, so the winner is not "
                    "meaningfully better."
                ),
                rules_fired=["tie_margin"],
            )

        if link.risk is None:
            return PolicyDecision(
                decision=MatchDecision.HUMAN_REVIEW,
                reason="No calibrated risk estimate is available for this candidate.",
                rules_fired=["missing_risk"],
            )

        max_risk = self.effective_max_risk
        if link.risk > max_risk:
            return PolicyDecision(
                decision=MatchDecision.HUMAN_REVIEW,
                reason=(
                    f"Estimated error probability {link.risk:.3f} exceeds the permitted "
                    f"{max_risk:.3f}."
                ),
                rules_fired=["risk_budget"],
            )
        fired.append("risk_budget_ok")

        min_score = float(automation.get("min_score", 0.9))
        if (link.score or 0.0) < min_score:
            return PolicyDecision(
                decision=MatchDecision.HUMAN_REVIEW,
                reason=f"Score {link.score or 0:.3f} is below the minimum {min_score:.2f}.",
                rules_fired=[*fired, "min_score"],
            )
        fired.append("min_score_ok")

        if link.advisory_invariants and review.get("force_on_advisory_failure", True):
            return PolicyDecision(
                decision=MatchDecision.HUMAN_REVIEW,
                reason="An advisory check failed: " + ", ".join(link.advisory_invariants),
                rules_fired=[*fired, "advisory_failure"],
            )

        competing_limit = int(review.get("force_when_competing_candidates_above", 3))
        competing = int(link.features.get("competing_candidates", 1))
        if competing > competing_limit:
            return PolicyDecision(
                decision=MatchDecision.HUMAN_REVIEW,
                reason=(
                    f"{competing} records compete for this link, above the limit of "
                    f"{competing_limit}."
                ),
                rules_fired=[*fired, "competing_candidates"],
            )

        if automation.get("require_evidence", True):
            supporting = sum(1 for draft in link.evidence if draft.supports)
            required = int(automation.get("min_supporting_evidence", 1))
            if supporting < required:
                return PolicyDecision(
                    decision=MatchDecision.HUMAN_REVIEW,
                    reason=f"Only {supporting} supporting evidence item(s); {required} required.",
                    rules_fired=[*fired, "insufficient_evidence"],
                )
            fired.append("evidence_ok")

        if abs(link.allocated_subunits) > int(automation.get("high_value_review_subunits", 0)):
            return PolicyDecision(
                decision=MatchDecision.HUMAN_REVIEW,
                reason="Amount is above the high-value review threshold.",
                rules_fired=[*fired, "high_value"],
            )

        return PolicyDecision(
            decision=MatchDecision.AUTO_ACCEPTED,
            reason=(
                f"Estimated error probability {link.risk:.4f} is within the permitted "
                f"{max_risk:.4f}, with supporting evidence and no failing checks."
            ),
            rules_fired=fired,
        )

    # -- agent budget ------------------------------------------------------

    def agent_budget(self) -> AgentBudget:
        agent = self.document["agent"]
        return AgentBudget(
            max_iterations=int(agent["max_iterations"]),
            max_tool_calls=int(agent["max_tool_calls"]),
            max_output_retries=int(agent["max_output_retries"]),
            allowed_tools=frozenset(agent["allowed_tools"]),
            min_cited_evidence=int(agent["min_cited_evidence"]),
            max_rows_per_search=int(agent["max_rows_per_search"]),
            requires_human_approval=bool(agent["recommendation_requires_human_approval"]),
        )

    def tool_allowed(self, tool_name: str) -> bool:
        return tool_name in self.agent_budget().allowed_tools

    # -- model promotion ---------------------------------------------------

    def evaluate_promotion(
        self, metrics: dict[str, Any], incumbent: dict[str, Any] | None
    ) -> tuple[bool, list[str]]:
        """Decide whether a challenger model may be promoted.

        Returns the verdict and the list of gates it failed. Approval is still
        required from a human afterwards; passing the gates only makes promotion
        eligible.
        """
        rules = self.document["model_promotion"]
        failures: list[str] = []

        bound = float(metrics.get("precision_lower_bound", 0.0))
        minimum = float(rules["min_precision_lower_bound"])
        if bound < minimum:
            failures.append(
                f"precision lower bound {bound:.4f} is below the required {minimum:.4f}"
            )

        if incumbent and rules.get("require_no_regression_in_precision", True):
            incumbent_bound = float(incumbent.get("precision_lower_bound", 0.0))
            if bound < incumbent_bound:
                failures.append(
                    f"precision lower bound {bound:.4f} regresses against the incumbent's "
                    f"{incumbent_bound:.4f}"
                )

        if incumbent:
            improvement = float(metrics.get("coverage", 0.0)) - float(
                incumbent.get("coverage", 0.0)
            )
            required = float(rules.get("min_coverage_improvement", 0.0))
            if improvement < required:
                failures.append(
                    f"coverage change {improvement:+.4f} does not meet the required {required:+.4f}"
                )

        return (not failures), failures
