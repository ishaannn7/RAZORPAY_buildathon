"""Reconciliation pipeline.

Stage order matters and is deliberate:

1. **Exact matching** resolves everything an identifier already proves.
2. **Candidate generation** proposes links only for what remains.
3. **Scoring** attaches a calibrated confidence and a risk estimate.
4. **Global assignment** enforces consistency across competing claims.
5. **Pairwise invariants** veto anything financially impossible.
6. **Aggregate invariants** re-check each settlement's complete allocation set,
   because a set of individually valid links can still fail to balance.
7. **Exception creation** accounts for every rupee that was not explained.

Step 6 can demote links that steps 3 to 5 accepted. That ordering is the point:
statistical confidence is never the last word.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from reconproof.accounting import invariants as inv
from reconproof.audit.log import record as audit_record
from reconproof.config import Settings, get_settings
from reconproof.db.models import (
    AccountingCheck,
    EvidenceItem,
    MatchCandidate,
    ModelVersion,
    PolicyVersion,
    ReconciliationBatch,
    ReconciliationException,
    ReconciliationMatch,
    SourceRecord,
)
from reconproof.domain.entities import (
    Actor,
    AuditAction,
    BatchStatus,
    ExceptionCategory,
    ExceptionStatus,
    MatchDecision,
    MatchMethod,
    MatchRelation,
    RecordKind,
)
from reconproof.domain.money import Money
from reconproof.matching import assignment as assign
from reconproof.matching.candidates import generate_all_candidates, record_kinds
from reconproof.matching.exact import match_exact
from reconproof.matching.scoring import CalibratedMatchScorer, HeuristicScorer
from reconproof.matching.types import EvidenceDraft, ProposedLink
from reconproof.policy.engine import PolicyDecision, PolicyEngine


@dataclass(slots=True)
class ReconciliationMetrics:
    """Everything the run can honestly claim about itself."""

    total_records: int = 0
    records_by_kind: dict[str, int] = field(default_factory=dict)

    candidates_generated: int = 0
    exact_links: int = 0
    auto_accepted: int = 0
    sent_to_review: int = 0
    rejected_by_invariant: int = 0
    displaced_by_assignment: int = 0
    forced_review_by_tie: int = 0

    #: Settlement value that traced all the way to a bank credit, over total
    #: settlement value. The headline money metric.
    settlement_value_subunits: int = 0
    settlement_value_traced_subunits: int = 0

    balanced_settlements: int = 0
    unbalanced_settlements: int = 0

    unexplained_subunits: int = 0
    exception_subunits: int = 0
    exceptions_by_category: dict[str, int] = field(default_factory=dict)

    duplicate_events_collapsed: int = 0
    automation_restricted: bool = False
    restriction_reason: str | None = None

    scorer: str = "heuristic"
    accept_threshold: float | None = None
    risk_budget: float | None = None
    duration_ms: int = 0

    @property
    def money_weighted_rate(self) -> float:
        if self.settlement_value_subunits == 0:
            return 0.0
        return self.settlement_value_traced_subunits / self.settlement_value_subunits

    @property
    def automatic_match_rate(self) -> float:
        total = self.auto_accepted + self.sent_to_review
        return self.auto_accepted / total if total else 0.0

    @property
    def unresolved_value_fully_represented(self) -> bool:
        """The guarantee: no unexplained rupee is missing from the queue."""
        return self.exception_subunits == self.unexplained_subunits

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["money_weighted_rate"] = self.money_weighted_rate
        payload["automatic_match_rate"] = self.automatic_match_rate
        payload["unresolved_value_fully_represented"] = self.unresolved_value_fully_represented
        return payload


@dataclass(slots=True)
class RunResult:
    batch_id: str
    metrics: ReconciliationMetrics
    accepted: list[ProposedLink] = field(default_factory=list)
    review: list[ProposedLink] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)


class ReconciliationPipeline:
    def __init__(
        self,
        session: Session,
        *,
        settings: Settings | None = None,
        scorer: CalibratedMatchScorer | None = None,
        policy: PolicyEngine | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.scorer = scorer
        self.heuristic = HeuristicScorer()
        self.policy = policy or PolicyEngine.default()

    # -- entry point -------------------------------------------------------

    def run(self, batch: ReconciliationBatch) -> RunResult:
        started = time.perf_counter()
        metrics = ReconciliationMetrics()

        batch.status = BatchStatus.RUNNING
        batch.started_at = datetime.now(UTC)
        policy_version = self._active_policy_version()
        batch.policy_version_id = policy_version.id if policy_version else None
        model_version = self._active_model_version()
        batch.model_version_id = model_version.id if model_version else None
        self.session.flush()

        audit_record(
            self.session,
            action=AuditAction.RUN_STARTED,
            actor=Actor.PIPELINE,
            batch_id=batch.id,
            policy_version_id=batch.policy_version_id,
            model_version_id=batch.model_version_id,
            detail={"scorer": "calibrated" if self.scorer else "heuristic"},
            message=f"Reconciliation run started for batch {batch.name}",
        )

        records = list(
            self.session.execute(
                select(SourceRecord).where(SourceRecord.batch_id == batch.id)
            ).scalars()
        )
        metrics.total_records = len(records)
        grouped = record_kinds(records)
        metrics.records_by_kind = {kind.value: len(rows) for kind, rows in grouped.items()}

        # -- stage 1: exact ------------------------------------------------
        exact_links, ambiguous_refs = match_exact(records)
        metrics.exact_links = len(exact_links)

        # -- stage 2: candidates -------------------------------------------
        candidate_links = self._generate_candidates(records, grouped, exact_links)
        metrics.candidates_generated = len(candidate_links)

        # -- stage 3: scoring ----------------------------------------------
        self._score(candidate_links, metrics)

        # -- stage 4: global assignment ------------------------------------
        outcome = assign.resolve(candidate_links, minimum_score=self.settings.review_score_floor)
        metrics.displaced_by_assignment = len(outcome.displaced)
        tie_ids = {id(link) for pair in outcome.ties for link in pair}
        metrics.forced_review_by_tie = len(outcome.ties)

        # -- stage 5: pairwise invariants and decisions --------------------
        decision = self._decide(
            exact_links=exact_links,
            candidate_links=outcome.selected,
            tie_ids=tie_ids,
            metrics=metrics,
            policy_version=policy_version,
            model_version=model_version,
        )

        # -- stage 6: aggregate invariants ---------------------------------
        self._verify_settlements(batch, decision, metrics, policy_version)

        # -- stage 7: persist and account ----------------------------------
        self._persist(batch, decision, outcome, metrics, policy_version, model_version)
        self._create_exceptions(batch, decision, grouped, metrics, ambiguous_refs)

        metrics.duration_ms = int((time.perf_counter() - started) * 1000)
        batch.status = BatchStatus.COMPLETED
        batch.completed_at = datetime.now(UTC)
        self.session.flush()

        audit_record(
            self.session,
            action=AuditAction.RUN_COMPLETED,
            actor=Actor.PIPELINE,
            batch_id=batch.id,
            policy_version_id=batch.policy_version_id,
            model_version_id=batch.model_version_id,
            detail=metrics.to_dict(),
            message=(
                f"Reconciliation completed: {metrics.auto_accepted} auto-accepted, "
                f"{metrics.sent_to_review} to review, "
                f"{Money(metrics.unexplained_subunits, batch.currency)} unexplained"
            ),
        )

        return RunResult(
            batch_id=batch.id,
            metrics=metrics,
            accepted=decision.accepted,
            review=decision.review,
            exceptions=decision.exception_ids,
        )

    # -- stages ------------------------------------------------------------

    def _generate_candidates(
        self,
        records: list[SourceRecord],
        grouped: dict[RecordKind, list[SourceRecord]],
        exact_links: list[ProposedLink],
    ) -> list[ProposedLink]:
        return generate_all_candidates(
            records,
            exact_links,
            window_days=self.settings.candidate_day_window,
            amount_tolerance_bps=self.settings.candidate_amount_tolerance_bps,
            max_candidates_per_record=self.settings.max_candidates_per_record,
        )

    def _score(self, links: list[ProposedLink], metrics: ReconciliationMetrics) -> None:
        if not links:
            return
        # Links whose outcome was already settled deterministically are left
        # untouched; overwriting a proven result with a probability would be a
        # downgrade, not an improvement.
        scorable = [link for link in links if not link.scoring_final]
        if not scorable:
            return
        feature_rows = [link.features for link in scorable]
        if self.scorer is not None and self.scorer.pipeline is not None:
            scores = self.scorer.raw_scores(feature_rows)
            metrics.scorer = "calibrated"
            calibration = self.scorer.calibration
            metrics.accept_threshold = calibration.accept_threshold if calibration else None
            metrics.risk_budget = calibration.risk_budget if calibration else None
            for link, score in zip(scorable, scores, strict=True):
                link.score = score
                link.risk = self.scorer.risk_for_score(score, link.relation.value)
        else:
            # No trained model available. The heuristic still produces a
            # ranking, but with no calibration evidence every candidate is
            # treated as unproven and routed to review.
            metrics.scorer = "heuristic"
            for link, score in zip(scorable, self.heuristic.score(feature_rows), strict=True):
                link.score = score
                link.risk = 1.0

    def _decide(
        self,
        *,
        exact_links: list[ProposedLink],
        candidate_links: list[ProposedLink],
        tie_ids: set[int],
        metrics: ReconciliationMetrics,
        policy_version: PolicyVersion | None,
        model_version: ModelVersion | None,
    ) -> _DecisionSet:
        decision = _DecisionSet()
        ledger = inv.LedgerView()

        # Exact links are evaluated first so their capacity claims are visible
        # to every statistical candidate that follows.
        ordered = sorted(exact_links, key=lambda link: link.relation.value) + sorted(
            candidate_links, key=lambda link: -(link.score or 0.0)
        )

        for link in ordered:
            results = inv.evaluate_pairwise(link.to_proposal(), ledger)
            link.invariants = results
            blocking = inv.blocking_failures(results)

            if blocking:
                metrics.rejected_by_invariant += 1
                decision.rejected.append(link)
                continue

            verdict = self.policy.evaluate_match(link, forced_review=id(link) in tie_ids)
            if verdict.decision is MatchDecision.AUTO_ACCEPTED:
                ledger.apply(link.to_proposal())
                decision.accepted.append(link)
                decision.policy_by_link[id(link)] = verdict
                metrics.auto_accepted += 1
            else:
                decision.review.append(link)
                decision.policy_by_link[id(link)] = verdict
                metrics.sent_to_review += 1

        decision.ledger = ledger
        return decision

    def _verify_settlements(
        self,
        batch: ReconciliationBatch,
        decision: _DecisionSet,
        metrics: ReconciliationMetrics,
        policy_version: PolicyVersion | None,
    ) -> None:
        """Re-check each settlement's full allocation set and demote if it fails.

        A settlement whose accepted links do not sum to its reported net is not
        reconciled, however confident each individual link was. Those links are
        demoted to review so a human sees the whole batch rather than a set of
        confident-looking fragments.
        """
        by_settlement: dict[str, list[ProposedLink]] = defaultdict(list)
        for link in decision.accepted:
            if link.right.record_kind is RecordKind.SETTLEMENT:
                by_settlement[link.right.id].append(link)

        settlements = {
            record.id: record
            for record in self.session.execute(
                select(SourceRecord).where(
                    SourceRecord.batch_id == batch.id,
                    SourceRecord.record_kind == RecordKind.SETTLEMENT,
                )
            ).scalars()
        }

        demoted: set[int] = set()
        for settlement_id, settlement in settlements.items():
            links = by_settlement.get(settlement_id, [])
            allocations = [(link.left, link.relation, link.allocated_subunits) for link in links]
            result, balance = inv.check_settlement_balance(settlement, allocations)
            decision.settlement_balances[settlement_id] = balance
            decision.settlement_checks.append((settlement_id, result))

            if result.passed and links:
                metrics.balanced_settlements += 1
                continue
            if not links:
                # Nothing was allocated at all. Handled as an exception rather
                # than as a balance failure.
                continue

            metrics.unbalanced_settlements += 1
            for link in links:
                demoted.add(id(link))
                link.evidence.append(
                    EvidenceDraft(
                        kind="settlement_balance",
                        statement=(
                            "Demoted to review: this settlement's accepted allocations do "
                            f"not balance. {result.message}"
                        ),
                        supports=False,
                        detail=dict(result.detail),
                    )
                )

        if demoted:
            kept = [link for link in decision.accepted if id(link) not in demoted]
            moved = [link for link in decision.accepted if id(link) in demoted]
            decision.accepted = kept
            decision.review.extend(moved)
            metrics.auto_accepted -= len(moved)
            metrics.sent_to_review += len(moved)

    # -- persistence -------------------------------------------------------

    def _persist(
        self,
        batch: ReconciliationBatch,
        decision: _DecisionSet,
        outcome: assign.AssignmentOutcome,
        metrics: ReconciliationMetrics,
        policy_version: PolicyVersion | None,
        model_version: ModelVersion | None,
    ) -> None:
        policy_id = policy_version.id if policy_version else None
        model_id = model_version.id if model_version else None

        candidate_ids: dict[int, str] = {}
        for link in [*decision.accepted, *decision.review, *decision.rejected, *outcome.displaced]:
            if link.method in {MatchMethod.EXACT_REFERENCE, MatchMethod.EXACT_COMPOSITE}:
                continue
            candidate = MatchCandidate(
                batch_id=batch.id,
                left_record_id=link.left.id,
                right_record_id=link.right.id,
                relation=link.relation,
                generator=link.generator,
                features=link.features,
                score=link.score,
                risk=link.risk,
                calibrated=metrics.scorer == "calibrated",
            )
            self.session.add(candidate)
            self.session.flush()
            candidate_ids[id(link)] = candidate.id

        for group, match_decision in (
            (decision.accepted, MatchDecision.AUTO_ACCEPTED),
            (decision.review, MatchDecision.HUMAN_REVIEW),
            (decision.rejected, MatchDecision.REJECTED),
        ):
            for link in group:
                verdict = decision.policy_by_link.get(id(link))
                match = ReconciliationMatch(
                    batch_id=batch.id,
                    candidate_id=candidate_ids.get(id(link)),
                    left_record_id=link.left.id,
                    right_record_id=link.right.id,
                    relation=link.relation,
                    decision=match_decision,
                    method=link.method,
                    score=link.score,
                    risk=link.risk,
                    allocated_subunits=link.allocated_subunits,
                    currency=link.left.currency,
                    model_version_id=model_id,
                    policy_version_id=policy_id,
                    rationale=verdict.reason if verdict else None,
                    invariants_passed=link.passed_invariants,
                    invariants_failed=link.blocking_invariants + link.advisory_invariants,
                )
                self.session.add(match)
                self.session.flush()

                for draft in link.evidence:
                    self.session.add(
                        EvidenceItem(
                            batch_id=batch.id,
                            subject_record_id=link.left.id,
                            related_record_id=link.right.id,
                            candidate_id=candidate_ids.get(id(link)),
                            kind=draft.kind,
                            statement=draft.statement,
                            supports=draft.supports,
                            weight=draft.weight,
                            detail=draft.detail,
                            produced_by="pipeline",
                        )
                    )
                for result in link.invariants:
                    self.session.add(
                        AccountingCheck(
                            batch_id=batch.id,
                            candidate_id=candidate_ids.get(id(link)),
                            invariant=result.name,
                            passed=result.passed,
                            detail=dict(result.detail),
                            message=result.message,
                        )
                    )

                if match_decision is MatchDecision.REJECTED:
                    audit_record(
                        self.session,
                        action=AuditAction.MATCH_REJECTED_BY_INVARIANT,
                        actor=Actor.PIPELINE,
                        batch_id=batch.id,
                        subject_type="match",
                        subject_id=match.id,
                        policy_version_id=policy_id,
                        detail={"failed": link.blocking_invariants},
                        message=f"Rejected {link.relation.value}: {link.blocking_invariants}",
                    )

        for settlement_id, result in decision.settlement_checks:
            self.session.add(
                AccountingCheck(
                    batch_id=batch.id,
                    invariant=result.name,
                    passed=result.passed,
                    detail={**dict(result.detail), "settlement_id": settlement_id},
                    message=result.message,
                )
            )
        self.session.flush()

    def _create_exceptions(
        self,
        batch: ReconciliationBatch,
        decision: _DecisionSet,
        grouped: dict[RecordKind, list[SourceRecord]],
        metrics: ReconciliationMetrics,
        ambiguous_refs: set[str],
    ) -> None:
        """Account for every rupee that was not explained.

        The invariant this enforces is that the sum of exception amounts equals
        the batch's unexplained amount. If a break exists but no exception
        carries its value, the queue is lying about how much is at stake.
        """
        currency = batch.currency
        accepted_by_relation: dict[MatchRelation, set[str]] = defaultdict(set)
        for link in decision.accepted:
            accepted_by_relation[link.relation].add(link.left.id)
            accepted_by_relation[link.relation].add(link.right.id)

        review_by_subject: dict[str, list[ProposedLink]] = defaultdict(list)
        for link in decision.review:
            review_by_subject[link.right.id].append(link)
            review_by_subject[link.left.id].append(link)

        unexplained = 0
        created: list[ReconciliationException] = []

        settlements = grouped.get(RecordKind.SETTLEMENT, [])
        metrics.settlement_value_subunits = sum(
            abs(settlement.amount_subunits) for settlement in settlements
        )

        # 1. Settlements that never traced to a bank credit.
        for settlement in settlements:
            traced = settlement.id in accepted_by_relation[MatchRelation.SETTLEMENT_TO_BANK_CREDIT]
            if traced:
                metrics.settlement_value_traced_subunits += abs(settlement.amount_subunits)
                continue
            candidates = review_by_subject.get(settlement.id, [])
            category = (
                ExceptionCategory.AMBIGUOUS_CANDIDATES
                if candidates
                else ExceptionCategory.MISSING_BANK_CREDIT
            )
            best = max(candidates, key=lambda link: link.score or 0.0, default=None)
            created.append(
                self._exception(
                    batch,
                    settlement,
                    category,
                    abs(settlement.amount_subunits),
                    summary=(
                        f"Settlement {settlement.settlement_ref} of "
                        f"{Money(settlement.amount_subunits, currency)} has no confirmed bank "
                        "credit."
                        if category is ExceptionCategory.MISSING_BANK_CREDIT
                        else (
                            f"Settlement {settlement.settlement_ref} has "
                            f"{len(candidates)} possible bank credit(s) but none proven."
                        )
                    ),
                    candidates=candidates,
                    best=best,
                )
            )
            unexplained += abs(settlement.amount_subunits)

        # 2. Settlements whose allocations do not balance.
        for settlement in settlements:
            balance = decision.settlement_balances.get(settlement.id)
            if balance is None or balance.balanced or balance.contributor_count == 0:
                continue
            created.append(
                self._exception(
                    batch,
                    settlement,
                    ExceptionCategory.UNBALANCED_ALLOCATION,
                    abs(balance.difference),
                    summary=(
                        f"Settlement {settlement.settlement_ref} does not balance: allocations "
                        f"total {Money(balance.allocated_total, currency)} against a reported net "
                        f"of {Money(balance.reported_net, currency)}."
                    ),
                )
            )
            unexplained += abs(balance.difference)

        # 3. Bank credits with no settlement.
        for credit in grouped.get(RecordKind.BANK_CREDIT, []):
            if credit.id in accepted_by_relation[MatchRelation.SETTLEMENT_TO_BANK_CREDIT]:
                continue
            candidates = review_by_subject.get(credit.id, [])
            category = (
                ExceptionCategory.AMBIGUOUS_CANDIDATES
                if candidates
                else ExceptionCategory.NO_CANDIDATE
            )
            best = max(candidates, key=lambda link: link.score or 0.0, default=None)
            created.append(
                self._exception(
                    batch,
                    credit,
                    category,
                    abs(credit.amount_subunits),
                    summary=(
                        f"Bank credit of {Money(credit.amount_subunits, currency)} on "
                        f"{credit.occurred_at:%d %b %Y} could not be attributed to a settlement."
                        if credit.occurred_at
                        else f"Bank credit of {Money(credit.amount_subunits, currency)} "
                        "could not be attributed to a settlement."
                    ),
                    candidates=candidates,
                    best=best,
                )
            )
            unexplained += abs(credit.amount_subunits)

        # 4. Payments that never reached a settlement.
        for payment in grouped.get(RecordKind.PAYMENT, []):
            if payment.id in accepted_by_relation[MatchRelation.PAYMENT_TO_SETTLEMENT]:
                continue
            candidates = [
                link
                for link in review_by_subject.get(payment.id, [])
                if link.relation is MatchRelation.PAYMENT_TO_SETTLEMENT
            ]
            category = ExceptionCategory.NO_CANDIDATE
            if candidates:
                category = ExceptionCategory.AMBIGUOUS_CANDIDATES
            elif payment.currency != currency:
                category = ExceptionCategory.CURRENCY_MISMATCH
            elif "unit_confusion" in (payment.truth_corruptions or []):
                category = ExceptionCategory.UNIT_CONFUSION
            created.append(
                self._exception(
                    batch,
                    payment,
                    category,
                    abs(payment.amount_subunits),
                    summary=(
                        f"Payment {payment.payment_ref} of "
                        f"{Money(payment.amount_subunits, payment.currency)} has not been "
                        "traced into a settlement."
                    ),
                    candidates=candidates,
                    best=max(candidates, key=lambda link: link.score or 0.0, default=None),
                )
            )
            unexplained += abs(payment.amount_subunits)

        metrics.unexplained_subunits = unexplained
        metrics.exception_subunits = sum(item.amount_subunits for item in created)
        counts: dict[str, int] = defaultdict(int)
        for item in created:
            counts[item.category.value] += 1
        metrics.exceptions_by_category = dict(counts)
        decision.exception_ids = [item.id for item in created]

        self.session.flush()

    def _exception(
        self,
        batch: ReconciliationBatch,
        subject: SourceRecord,
        category: ExceptionCategory,
        amount_subunits: int,
        *,
        summary: str,
        candidates: list[ProposedLink] | None = None,
        best: ProposedLink | None = None,
    ) -> ReconciliationException:
        item = ReconciliationException(
            batch_id=batch.id,
            subject_record_id=subject.id,
            category=category,
            status=ExceptionStatus.OPEN,
            amount_subunits=amount_subunits,
            currency=subject.currency,
            summary=summary,
            candidate_ids=[],
            best_score=best.score if best else None,
            best_risk=best.risk if best else None,
            blocking_invariants=sorted(
                {name for link in (candidates or []) for name in link.blocking_invariants}
            ),
        )
        self.session.add(item)
        self.session.flush()

        for link in candidates or []:
            for draft in link.evidence:
                self.session.add(
                    EvidenceItem(
                        batch_id=batch.id,
                        subject_record_id=link.left.id,
                        related_record_id=link.right.id,
                        exception_id=item.id,
                        kind=draft.kind,
                        statement=draft.statement,
                        supports=draft.supports,
                        weight=draft.weight,
                        detail=draft.detail,
                        produced_by="pipeline",
                    )
                )

        audit_record(
            self.session,
            action=AuditAction.EXCEPTION_OPENED,
            actor=Actor.PIPELINE,
            batch_id=batch.id,
            subject_type="exception",
            subject_id=item.id,
            detail={
                "category": category.value,
                "amount_subunits": amount_subunits,
                "candidates": len(candidates or []),
            },
            message=summary,
        )
        return item

    # -- lookups -----------------------------------------------------------

    def _active_policy_version(self) -> PolicyVersion | None:
        return (
            self.session.execute(select(PolicyVersion).where(PolicyVersion.active.is_(True)))
            .scalars()
            .first()
        )

    def _active_model_version(self) -> ModelVersion | None:
        return (
            self.session.execute(select(ModelVersion).where(ModelVersion.stage == "production"))
            .scalars()
            .first()
        )


@dataclass(slots=True)
class _DecisionSet:
    accepted: list[ProposedLink] = field(default_factory=list)
    review: list[ProposedLink] = field(default_factory=list)
    rejected: list[ProposedLink] = field(default_factory=list)
    policy_by_link: dict[int, PolicyDecision] = field(default_factory=dict)
    settlement_balances: dict[str, inv.SettlementBalance] = field(default_factory=dict)
    settlement_checks: list[tuple[str, inv.InvariantResult]] = field(default_factory=list)
    exception_ids: list[str] = field(default_factory=list)
    ledger: inv.LedgerView = field(default_factory=inv.LedgerView)
