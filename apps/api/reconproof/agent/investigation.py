"""The bounded investigation workflow.

An explicit state machine, not a framework graph. LangGraph was the obvious
candidate and was deliberately not used: the value it adds here is orchestration
sugar over eight fixed transitions, while what this workflow actually needs is a
persisted step log, a hard tool budget and a verifier that can veto the model.
All three are simpler to test and to audit written directly, and every phase
transition is already a row in ``agent_steps``, which is the checkpointing that
matters for an audit replay.

The phases run in a fixed order and cannot loop back arbitrarily:

    TRIAGE -> PLAN -> GATHER_EVIDENCE -> GENERATE_HYPOTHESES
           -> VERIFY -> SELF_CRITIQUE -> RECOMMEND | ABSTAIN

Only two things can leave this module with any effect: a recommendation attached
to an exception awaiting human approval, or an abstention with its reason. The
agent has no path to the ledger.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from reconproof.agent.providers.base import (
    Hypothesis,
    InvestigationBrief,
    ReasoningProvider,
)
from reconproof.agent.providers.deterministic import DeterministicProvider
from reconproof.agent.providers.registry import resolve_provider
from reconproof.agent.tools import (
    InvestigationTools,
    ToolDenied,
    ToolInputError,
    ToolResult,
    _record_view,
)
from reconproof.agent.verifier import VerificationResult, verify_hypothesis
from reconproof.audit.log import record as audit_record
from reconproof.config import Settings, get_settings
from reconproof.db.models import (
    AgentRun,
    AgentStep,
    EvidenceItem,
    MatchCandidate,
    ReconciliationException,
    SourceRecord,
    ToolCall,
)
from reconproof.domain.entities import (
    Actor,
    AgentOutcome,
    AgentPhase,
    AuditAction,
    ExceptionStatus,
)
from reconproof.domain.money import Money
from reconproof.policy.engine import PolicyEngine

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class InvestigationOutcome:
    run_id: str
    outcome: AgentOutcome
    phase: AgentPhase
    recommendation: dict[str, Any] | None = None
    abstain_reason: str | None = None
    verification: VerificationResult | None = None
    steps: int = 0
    tool_calls: int = 0
    denied_tool_calls: int = 0


@dataclass(slots=True)
class _State:
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    sequence: int = 0


class Investigator:
    """Runs one bounded investigation of one exception."""

    def __init__(
        self,
        session: Session,
        *,
        settings: Settings | None = None,
        provider: ReasoningProvider | None = None,
        policy: PolicyEngine | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.policy = policy or PolicyEngine.default()
        self.provider = provider or resolve_provider(self.settings)
        self.budget = self.policy.agent_budget()

    # -- entry point -------------------------------------------------------

    def investigate(self, exception: ReconciliationException) -> InvestigationOutcome:
        started = time.perf_counter()
        run = AgentRun(
            batch_id=exception.batch_id,
            exception_id=exception.id,
            kind="investigation",
            phase=AgentPhase.TRIAGE,
            provider=self.provider.name,
            model_name=self.provider.model_name,
        )
        self.session.add(run)
        self.session.flush()

        audit_record(
            self.session,
            action=AuditAction.AGENT_RUN_STARTED,
            actor=Actor.AGENT,
            batch_id=exception.batch_id,
            subject_type="exception",
            subject_id=exception.id,
            agent_run_id=run.id,
            actor_detail=f"{self.provider.name}:{self.provider.model_name or 'rules'}",
            detail={"category": exception.category.value},
            message=f"Investigation started for exception {exception.id}",
        )

        exception.status = ExceptionStatus.INVESTIGATING
        tools = InvestigationTools(
            self.session, exception=exception, policy=self.policy, budget=self.budget
        )
        state = _State()

        try:
            outcome = self._run_phases(run, exception, tools, state)
        except Exception as exc:
            logger.exception("agent.failed", exception_id=exception.id)
            run.phase = AgentPhase.FAILED
            run.outcome = AgentOutcome.TOOL_FAILURE
            run.abstain_reason = f"Investigation failed: {exc}"
            exception.status = ExceptionStatus.OPEN
            outcome = InvestigationOutcome(
                run_id=run.id,
                outcome=AgentOutcome.TOOL_FAILURE,
                phase=AgentPhase.FAILED,
                abstain_reason=run.abstain_reason,
            )

        self._persist_tool_calls(run, tools)
        run.iterations = state.sequence
        run.tool_calls = sum(1 for _, _, allowed, _ in tools.calls if allowed)
        run.denied_tool_calls = sum(1 for _, _, allowed, _ in tools.calls if not allowed)
        run.duration_ms = int((time.perf_counter() - started) * 1000)
        run.completed_at = datetime.now(UTC)
        run.rejected_hypotheses = state.rejected
        outcome.tool_calls = run.tool_calls
        outcome.denied_tool_calls = run.denied_tool_calls
        outcome.steps = state.sequence
        self.session.flush()
        return outcome

    # -- phases ------------------------------------------------------------

    def _run_phases(
        self,
        run: AgentRun,
        exception: ReconciliationException,
        tools: InvestigationTools,
        state: _State,
    ) -> InvestigationOutcome:
        subject = self.session.get(SourceRecord, exception.subject_record_id)
        if subject is None:  # pragma: no cover - foreign keys
            raise RuntimeError("exception subject record is missing")

        # --- TRIAGE: assemble the case from the database, deterministically.
        self._load_context(exception, state)
        self._step(
            run,
            state,
            AgentPhase.TRIAGE,
            thought=(
                f"Exception categorised {exception.category.value} for "
                f"{Money(exception.amount_subunits, exception.currency)}; "
                f"{len(state.candidates)} candidate(s) and "
                f"{len(state.evidence)} evidence item(s) on file."
            ),
            output={
                "category": exception.category.value,
                "candidates": len(state.candidates),
                "evidence": len(state.evidence),
            },
        )

        brief = self._brief(exception, subject, state)

        # --- PLAN
        plan = self.provider.plan(brief)
        self._step(
            run,
            state,
            AgentPhase.PLAN,
            thought=plan.thought,
            output={
                "requested_tools": [
                    {"tool": request.tool, "reason": request.reason}
                    for request in plan.tool_requests
                ]
            },
        )

        # --- GATHER_EVIDENCE
        for request in plan.tool_requests:
            if state.sequence >= self.budget.max_iterations:
                break
            try:
                result = tools.call(request.tool, **request.arguments)
            except ToolDenied as exc:
                # Recorded as a safety event: the agent reached for something it
                # was not allowed to have.
                audit_record(
                    self.session,
                    action=AuditAction.AGENT_TOOL_DENIED,
                    actor=Actor.AGENT,
                    batch_id=exception.batch_id,
                    subject_type="exception",
                    subject_id=exception.id,
                    agent_run_id=run.id,
                    detail={"tool": request.tool, "reason": str(exc)},
                    message=f"Denied tool call {request.tool}: {exc}",
                )
                state.tool_results.append(
                    {"tool": request.tool, "denied": True, "reason": str(exc)}
                )
                continue
            except ToolInputError as exc:
                state.tool_results.append({"tool": request.tool, "error": str(exc), "rows": []})
                continue
            state.tool_results.append(
                {
                    "tool": result.tool,
                    "arguments": request.arguments,
                    "summary": result.summary,
                    "rows": result.rows[: self.budget.max_rows_per_search],
                    "detail": result.detail,
                }
            )
            self._absorb_evidence(result, state)

        self._step(
            run,
            state,
            AgentPhase.GATHER_EVIDENCE,
            thought=f"Collected {len(state.tool_results)} tool result(s).",
            output={"results": [entry.get("summary") for entry in state.tool_results]},
        )

        brief = self._brief(exception, subject, state)

        # --- GENERATE_HYPOTHESES
        hypothesis = self.provider.hypothesize(brief)
        if hypothesis is None:
            # A provider that returns nothing usable falls back to rules rather
            # than ending the investigation: the case still deserves an answer.
            fallback = DeterministicProvider()
            hypothesis = fallback.hypothesize(brief)
            if hypothesis is not None:
                run.invalid_outputs += 1
                self._step(
                    run,
                    state,
                    AgentPhase.GENERATE_HYPOTHESES,
                    thought=(
                        f"The {self.provider.name} provider returned no usable hypothesis; "
                        "falling back to deterministic ranking."
                    ),
                    output={"fallback": True},
                )

        if hypothesis is None:
            return self._abstain(
                run,
                exception,
                state,
                reason="No candidate could be proposed from the available evidence.",
                outcome=AgentOutcome.ABSTAINED,
            )

        self._step(
            run,
            state,
            AgentPhase.GENERATE_HYPOTHESES,
            thought=hypothesis.rationale,
            output={
                "candidate_id": hypothesis.candidate_id,
                "confidence": hypothesis.confidence,
                "cited_evidence_ids": hypothesis.cited_evidence_ids,
                "remaining_uncertainty": hypothesis.remaining_uncertainty,
            },
        )

        # --- VERIFY: the model's proposal is checked, not trusted.
        verification = verify_hypothesis(
            self.session, exception=exception, hypothesis=hypothesis, budget=self.budget
        )
        self._step(
            run,
            state,
            AgentPhase.VERIFY,
            thought=(
                "Verification passed."
                if verification.passed
                else "Verification failed: " + "; ".join(verification.failures)
            ),
            output={
                "passed": verification.passed,
                "failures": verification.failures,
                "warnings": verification.warnings,
                "verified_evidence": verification.verified_evidence_ids,
                "hallucinated_evidence": verification.hallucinated_evidence_ids,
                "invariants_failed": verification.invariant_names_failed,
            },
        )

        if not verification.passed:
            state.rejected.append(
                {
                    "candidate_id": hypothesis.candidate_id,
                    "reason": "; ".join(verification.failures),
                    "hallucinated_evidence": verification.hallucinated_evidence_ids,
                }
            )
            if verification.cited_hallucination:
                run.invalid_outputs += 1
                audit_record(
                    self.session,
                    action=AuditAction.AGENT_OUTPUT_REJECTED,
                    actor=Actor.AGENT,
                    batch_id=exception.batch_id,
                    subject_type="exception",
                    subject_id=exception.id,
                    agent_run_id=run.id,
                    detail={"hallucinated": verification.hallucinated_evidence_ids},
                    message="Rejected a recommendation citing evidence that does not exist",
                )
            return self._abstain(
                run,
                exception,
                state,
                reason="; ".join(verification.failures),
                outcome=AgentOutcome.INVALID_OUTPUT
                if verification.cited_hallucination
                else AgentOutcome.ABSTAINED,
                verification=verification,
            )

        # --- SELF_CRITIQUE
        critique = self.provider.critique(brief, hypothesis)
        self._step(
            run,
            state,
            AgentPhase.SELF_CRITIQUE,
            thought=critique.reason,
            output={"should_abstain": critique.should_abstain, "concerns": critique.concerns},
        )
        if critique.should_abstain:
            state.rejected.append(
                {
                    "candidate_id": hypothesis.candidate_id,
                    "reason": f"withdrawn on self-review: {critique.reason}",
                }
            )
            return self._abstain(
                run,
                exception,
                state,
                reason=critique.reason or "Withdrawn on self-review.",
                outcome=AgentOutcome.ABSTAINED,
                verification=verification,
            )

        # --- RECOMMEND
        return self._recommend(run, exception, state, hypothesis, verification, brief)

    # -- terminal states ---------------------------------------------------

    def _recommend(
        self,
        run: AgentRun,
        exception: ReconciliationException,
        state: _State,
        hypothesis: Hypothesis,
        verification: VerificationResult,
        brief: InvestigationBrief,
    ) -> InvestigationOutcome:
        recommendation = {
            "candidate_id": hypothesis.candidate_id,
            "confidence": hypothesis.confidence,
            "rationale": hypothesis.rationale,
            "allocated_subunits": verification.allocated_subunits,
            "verified_evidence_ids": verification.verified_evidence_ids,
            "invariants_passed": verification.invariant_names_passed,
            "warnings": verification.warnings,
            "remaining_uncertainty": hypothesis.remaining_uncertainty,
            "requires_human_approval": self.budget.requires_human_approval,
        }
        run.recommendation = recommendation
        run.cited_evidence_ids = verification.verified_evidence_ids
        run.outcome = AgentOutcome.RECOMMENDED
        run.phase = AgentPhase.HUMAN_REVIEW

        exception.status = ExceptionStatus.AWAITING_APPROVAL
        exception.explanation = self.provider.explain(brief, "recommended for approval")
        exception.explanation_provider = self.provider.name

        self._step(
            run,
            state,
            AgentPhase.RECOMMEND,
            thought="Recommendation prepared and queued for human approval.",
            output=recommendation,
        )
        audit_record(
            self.session,
            action=AuditAction.AGENT_RECOMMENDED,
            actor=Actor.AGENT,
            batch_id=exception.batch_id,
            subject_type="exception",
            subject_id=exception.id,
            agent_run_id=run.id,
            detail=recommendation,
            message=(
                f"Recommended candidate {hypothesis.candidate_id} for human approval "
                f"with {len(verification.verified_evidence_ids)} verified citation(s)"
            ),
        )
        return InvestigationOutcome(
            run_id=run.id,
            outcome=AgentOutcome.RECOMMENDED,
            phase=AgentPhase.HUMAN_REVIEW,
            recommendation=recommendation,
            verification=verification,
        )

    def _abstain(
        self,
        run: AgentRun,
        exception: ReconciliationException,
        state: _State,
        *,
        reason: str,
        outcome: AgentOutcome,
        verification: VerificationResult | None = None,
    ) -> InvestigationOutcome:
        run.outcome = outcome
        run.phase = AgentPhase.ABSTAIN
        run.abstain_reason = reason
        exception.status = ExceptionStatus.OPEN
        exception.explanation = reason
        exception.explanation_provider = self.provider.name

        self._step(
            run,
            state,
            AgentPhase.ABSTAIN,
            thought=reason,
            output={"outcome": outcome.value},
        )
        audit_record(
            self.session,
            action=AuditAction.AGENT_ABSTAINED,
            actor=Actor.AGENT,
            batch_id=exception.batch_id,
            subject_type="exception",
            subject_id=exception.id,
            agent_run_id=run.id,
            detail={"reason": reason, "outcome": outcome.value},
            message=f"Abstained: {reason}"[:500],
        )
        return InvestigationOutcome(
            run_id=run.id,
            outcome=outcome,
            phase=AgentPhase.ABSTAIN,
            abstain_reason=reason,
            verification=verification,
        )

    # -- helpers -----------------------------------------------------------

    def _load_context(self, exception: ReconciliationException, state: _State) -> None:
        candidates = list(
            self.session.execute(
                select(MatchCandidate)
                .where(
                    MatchCandidate.batch_id == exception.batch_id,
                    (MatchCandidate.left_record_id == exception.subject_record_id)
                    | (MatchCandidate.right_record_id == exception.subject_record_id),
                )
                .order_by(MatchCandidate.score.desc().nullslast())
                .limit(10)
            ).scalars()
        )
        for candidate in candidates:
            left = self.session.get(SourceRecord, candidate.left_record_id)
            right = self.session.get(SourceRecord, candidate.right_record_id)
            if left is None or right is None:
                continue
            state.candidates.append(
                {
                    "candidate_id": candidate.id,
                    "relation": candidate.relation.value,
                    "score": candidate.score,
                    "risk": candidate.risk,
                    "features": candidate.features,
                    "left": _record_view(left),
                    "right": _record_view(right),
                }
            )

        evidence = list(
            self.session.execute(
                select(EvidenceItem)
                .where(
                    EvidenceItem.batch_id == exception.batch_id,
                    (EvidenceItem.exception_id == exception.id)
                    | (
                        EvidenceItem.candidate_id.in_(
                            [candidate.id for candidate in candidates] or [""]
                        )
                    ),
                )
                .limit(60)
            ).scalars()
        )
        state.evidence = [
            {
                "evidence_id": item.id,
                "candidate_id": item.candidate_id,
                "kind": item.kind,
                "statement": item.statement,
                "supports": item.supports,
            }
            for item in evidence
        ]

    def _brief(
        self,
        exception: ReconciliationException,
        subject: SourceRecord,
        state: _State,
    ) -> InvestigationBrief:
        return InvestigationBrief(
            exception_id=exception.id,
            category=exception.category.value,
            subject=_record_view(subject),
            amount=str(Money(exception.amount_subunits, exception.currency)),
            summary=exception.summary,
            candidates=state.candidates,
            evidence=state.evidence,
            tool_results=state.tool_results,
            available_tools=sorted(self.budget.allowed_tools),
            policy={
                "min_cited_evidence": self.budget.min_cited_evidence,
                "recommendation_requires_human_approval": self.budget.requires_human_approval,
                "max_rows_per_search": self.budget.max_rows_per_search,
            },
        )

    def _absorb_evidence(self, result: ToolResult, state: _State) -> None:
        """Register evidence surfaced by a tool so citations can resolve."""
        known = {entry["evidence_id"] for entry in state.evidence}
        for entry in result.detail.get("evidence", []) if result.detail else []:
            evidence_id = entry.get("evidence_id")
            if evidence_id and evidence_id not in known:
                state.evidence.append(entry)
                known.add(evidence_id)

    def _step(
        self,
        run: AgentRun,
        state: _State,
        phase: AgentPhase,
        *,
        thought: str | None,
        output: dict[str, Any] | None,
    ) -> None:
        state.sequence += 1
        run.phase = phase
        self.session.add(
            AgentStep(
                run_id=run.id,
                sequence=state.sequence,
                phase=phase,
                thought=(thought or "")[:8000] or None,
                output=output,
            )
        )
        self.session.flush()

    def _persist_tool_calls(self, run: AgentRun, tools: InvestigationTools) -> None:
        for index, (name, arguments, allowed, denial) in enumerate(tools.calls, start=1):
            self.session.add(
                ToolCall(
                    run_id=run.id,
                    sequence=index,
                    tool_name=name,
                    arguments={
                        key: value
                        for key, value in arguments.items()
                        if isinstance(value, str | int | float | bool | type(None))
                    },
                    allowed=allowed,
                    denial_reason=denial,
                )
            )
        self.session.flush()
