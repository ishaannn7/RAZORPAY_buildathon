"""Provider interface for the reasoning step of an investigation.

The seam exists so the reconciliation engine's correctness never depends on
which model is installed. A provider proposes a plan, a hypothesis and a
critique; it never decides anything. Every proposal passes through the
deterministic verifier, which is what allows a local 4B model, a frontier API
model and a no-model fallback to be interchangeable without changing what the
system is willing to accept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class InvestigationBrief:
    """What the provider is told about the case.

    Deliberately narrow. The provider sees the exception, the candidates the
    pipeline already scored, and the tool results gathered so far — not the
    ground truth, not the whole batch, and not any writable handle.
    """

    exception_id: str
    category: str
    subject: dict[str, Any]
    amount: str
    summary: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    available_tools: list[str] = field(default_factory=list)
    policy: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolRequest:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass(slots=True)
class Plan:
    """The provider's proposed next actions."""

    thought: str
    tool_requests: list[ToolRequest] = field(default_factory=list)


@dataclass(slots=True)
class Hypothesis:
    """A proposed resolution. Never authoritative on its own."""

    #: ``None`` means the provider is proposing that no link should be made.
    candidate_id: str | None
    confidence: float
    rationale: str
    cited_evidence_ids: list[str] = field(default_factory=list)
    remaining_uncertainty: list[str] = field(default_factory=list)

    def clamp(self) -> Hypothesis:
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        return self


@dataclass(slots=True)
class Critique:
    """The provider's self-review of its own hypothesis."""

    should_abstain: bool
    reason: str
    concerns: list[str] = field(default_factory=list)


@runtime_checkable
class ReasoningProvider(Protocol):
    """The reasoning surface an investigation needs."""

    name: str
    model_name: str | None

    def available(self) -> bool:
        """Whether this provider can currently serve a request."""
        ...

    def plan(self, brief: InvestigationBrief) -> Plan:
        """Propose which tools to call next."""
        ...

    def hypothesize(self, brief: InvestigationBrief) -> Hypothesis | None:
        """Propose a resolution, or ``None`` to abstain."""
        ...

    def critique(self, brief: InvestigationBrief, hypothesis: Hypothesis) -> Critique:
        """Review a hypothesis for reasons not to act on it."""
        ...

    def explain(self, brief: InvestigationBrief, verdict: str) -> str:
        """Write a short reviewer-facing explanation."""
        ...
