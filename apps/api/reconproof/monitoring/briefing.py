"""Batch-health briefing.

A second, narrower agent than the exception investigator, operating at batch
level rather than record level. Its job is to answer "what happened in this run
and what should a controller look at first", which is a different question from
"which settlement does this credit belong to".

It has no tools that touch records and cannot approve anything. Every figure it
states is computed here and passed to it, so the narrative cannot contain a
number the system did not measure. The language model, when present, only
phrases the briefing; the deterministic path produces the same facts in plainer
prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reconproof.agent.providers.base import InvestigationBrief
from reconproof.agent.providers.registry import resolve_provider
from reconproof.config import Settings, get_settings
from reconproof.db.models import (
    AnomalyEvent,
    AuditEvent,
    BatchBriefing,
    DriftReport,
    ReconciliationBatch,
    ReconciliationException,
)
from reconproof.domain.entities import AuditAction, ExceptionStatus
from reconproof.domain.money import Money


@dataclass(slots=True)
class BatchFacts:
    """Measured facts about a run. Every briefing statement traces to one of these."""

    metrics: dict[str, Any]
    open_exceptions: int
    unresolved: int
    top_categories: list[tuple[str, int]]
    largest_exceptions: list[tuple[str, int]]
    anomalies: list[tuple[str, str, int]]
    drift: DriftReport | None
    currency: str

    def to_cited_metrics(self) -> dict[str, Any]:
        return {
            "automatic_match_rate": self.metrics.get("automatic_match_rate"),
            "money_weighted_rate": self.metrics.get("money_weighted_rate"),
            "auto_accepted": self.metrics.get("auto_accepted"),
            "sent_to_review": self.metrics.get("sent_to_review"),
            "balanced_settlements": self.metrics.get("balanced_settlements"),
            "unbalanced_settlements": self.metrics.get("unbalanced_settlements"),
            "open_exceptions": self.open_exceptions,
            "unresolved_subunits": self.unresolved,
            "top_exception_categories": dict(self.top_categories),
            "anomaly_kinds": {kind: count for kind, _severity, count in self.anomalies},
            "drift_detected": bool(self.drift and self.drift.drift_detected),
            "max_psi": self.drift.max_psi if self.drift else None,
        }


def collect_facts(session: Session, batch: ReconciliationBatch) -> BatchFacts:
    run_event = (
        session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.batch_id == batch.id,
                AuditEvent.action == AuditAction.RUN_COMPLETED,
            )
            .order_by(AuditEvent.sequence.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    metrics: dict[str, Any] = dict(run_event.detail) if run_event and run_event.detail else {}

    open_statuses = [
        ExceptionStatus.OPEN,
        ExceptionStatus.INVESTIGATING,
        ExceptionStatus.AWAITING_APPROVAL,
    ]
    open_count = session.execute(
        select(func.count())
        .select_from(ReconciliationException)
        .where(
            ReconciliationException.batch_id == batch.id,
            ReconciliationException.status.in_(open_statuses),
        )
    ).scalar_one()
    unresolved = session.execute(
        select(func.coalesce(func.sum(ReconciliationException.amount_subunits), 0)).where(
            ReconciliationException.batch_id == batch.id,
            ReconciliationException.status.in_(open_statuses),
        )
    ).scalar_one()

    categories = session.execute(
        select(ReconciliationException.category, func.count())
        .where(ReconciliationException.batch_id == batch.id)
        .group_by(ReconciliationException.category)
        .order_by(func.count().desc())
    ).all()
    largest = session.execute(
        select(ReconciliationException.summary, ReconciliationException.amount_subunits)
        .where(
            ReconciliationException.batch_id == batch.id,
            ReconciliationException.status.in_(open_statuses),
        )
        .order_by(ReconciliationException.amount_subunits.desc())
        .limit(3)
    ).all()
    anomalies = session.execute(
        select(AnomalyEvent.kind, AnomalyEvent.severity, func.count())
        .where(AnomalyEvent.batch_id == batch.id)
        .group_by(AnomalyEvent.kind, AnomalyEvent.severity)
        .order_by(func.count().desc())
    ).all()
    drift = (
        session.execute(
            select(DriftReport)
            .where(DriftReport.batch_id == batch.id)
            .order_by(DriftReport.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )

    return BatchFacts(
        metrics=metrics,
        open_exceptions=int(open_count),
        unresolved=int(unresolved),
        top_categories=[(str(category.value), int(count)) for category, count in categories],
        largest_exceptions=[(str(summary), int(amount)) for summary, amount in largest],
        anomalies=[(str(kind), str(severity), int(count)) for kind, severity, count in anomalies],
        drift=drift,
        currency=batch.currency,
    )


def _deterministic_briefing(
    batch: ReconciliationBatch, facts: BatchFacts
) -> tuple[str, str, list[str]]:
    metrics = facts.metrics
    rate = metrics.get("automatic_match_rate") or 0.0
    money_rate = metrics.get("money_weighted_rate") or 0.0
    unresolved = Money(facts.unresolved, facts.currency)

    headline = (
        f"{rate:.1%} of links were reconciled automatically and {money_rate:.1%} of "
        f"settlement value traced to a bank credit. {unresolved} remains unexplained "
        f"across {facts.open_exceptions} open exception(s)."
    )

    lines = [
        f"Batch '{batch.name}' processed {metrics.get('total_records', 0)} records, "
        f"accepting {metrics.get('auto_accepted', 0)} links automatically and routing "
        f"{metrics.get('sent_to_review', 0)} to review.",
        f"{metrics.get('balanced_settlements', 0)} settlement(s) balance exactly; "
        f"{metrics.get('unbalanced_settlements', 0)} do not.",
    ]
    if metrics.get("rejected_by_invariant"):
        lines.append(
            f"{metrics['rejected_by_invariant']} proposed link(s) were rejected outright "
            "for breaking an accounting invariant."
        )
    if facts.top_categories:
        top = ", ".join(f"{name} ({count})" for name, count in facts.top_categories[:4])
        lines.append(f"Exceptions are concentrated in: {top}.")
    if facts.largest_exceptions:
        summary, amount = facts.largest_exceptions[0]
        lines.append(f"The largest single item is {Money(amount, facts.currency)}: {summary}")
    if facts.anomalies:
        kinds = ", ".join(f"{kind} ({count})" for kind, _severity, count in facts.anomalies[:4])
        lines.append(f"Anomaly detectors reported: {kinds}.")
    if facts.drift and facts.drift.drift_detected:
        lines.append(facts.drift.summary or "Input drift was detected.")
    if not metrics.get("unresolved_value_fully_represented", True):
        lines.append(
            "Warning: the exception queue does not account for the full unexplained "
            "amount. Treat the unresolved figure as a lower bound."
        )

    actions: list[str] = []
    if facts.largest_exceptions:
        actions.append(
            f"Review the {len(facts.largest_exceptions)} largest open exception(s) first; "
            "they carry most of the unresolved value."
        )
    if metrics.get("unbalanced_settlements"):
        actions.append(
            "Investigate the unbalanced settlements: a fee or refund line is likely "
            "missing from the source files."
        )
    if any(kind == "settlement_without_bank_credit" for kind, _s, _c in facts.anomalies):
        actions.append(
            "Chase the settlements with no bank credit. This is money the provider "
            "reports as paid that has not been seen arriving."
        )
    if facts.drift and facts.drift.drift_detected:
        actions.append(
            "Re-calibrate on current data before relaxing automation; the present "
            "threshold was established on a different distribution."
        )
    if not actions:
        actions.append("No action required. The batch reconciled within expected bounds.")
    return headline, "\n".join(lines), actions


def build_briefing(
    session: Session,
    batch: ReconciliationBatch,
    *,
    settings: Settings | None = None,
) -> BatchBriefing:
    """Produce and persist a briefing for *batch*."""
    settings = settings or get_settings()
    facts = collect_facts(session, batch)
    headline, body, actions = _deterministic_briefing(batch, facts)

    provider = resolve_provider(settings)
    if provider.name != "deterministic":
        # The model only rephrases. It is given the computed facts and never the
        # raw records, so it cannot introduce a figure the system did not measure.
        brief = InvestigationBrief(
            exception_id=batch.id,
            category="batch_health",
            subject={"kind": "batch", "reference": batch.name},
            amount=str(Money(facts.unresolved, facts.currency)),
            summary=headline,
            tool_results=[{"tool": "batch_facts", "rows": [facts.to_cited_metrics()]}],
            available_tools=[],
            policy={},
        )
        narrative = provider.explain(brief, "batch health briefing")
        if narrative and len(narrative) > 40:
            body = f"{narrative}\n\n{body}"

    briefing = BatchBriefing(
        batch_id=batch.id,
        headline=headline,
        body=body,
        recommended_actions=actions,
        cited_metrics=facts.to_cited_metrics(),
        provider=provider.name,
    )
    session.add(briefing)
    session.flush()
    return briefing
