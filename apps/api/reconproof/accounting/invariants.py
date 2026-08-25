"""Accounting invariants.

These are the rules that hold regardless of what any model believes. A model
proposes a link; an invariant can veto it. Nothing reaches the ledger without
passing every applicable check, which is why the automatic-match path can be
trusted even though part of it is statistical.

Two scopes exist because they answer different questions:

* **Pairwise** invariants judge one proposed link in isolation — are the
  currencies compatible, is the date ordering possible, is there capacity left.
* **Aggregate** invariants judge a target record's *complete* allocation set —
  does this settlement's net actually equal the sum of what was allocated to it.
  A set of individually plausible links can still be collectively impossible,
  and only the aggregate scope can see that.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from reconproof.domain.entities import (
    MANY_TO_ONE_RELATIONS,
    RELATION_ENDPOINTS,
    MatchRelation,
    RecordKind,
)
from reconproof.domain.money import Money, looks_like_unit_confusion

if TYPE_CHECKING:
    from reconproof.db.models import SourceRecord

#: A payment settling more than this long after capture is not impossible, but
#: it is implausible enough that the link needs human eyes.
MAX_SETTLEMENT_LAG_DAYS = 21

#: A bank credit lands on or shortly after the settlement date. A credit
#: *before* the settlement it supposedly pays is a date-ordering violation.
MAX_BANK_CREDIT_LAG_DAYS = 7
BANK_CREDIT_EARLY_TOLERANCE_HOURS = 12

#: Clock-skew and IST-vs-UTC tolerance for orderings that should be strict.
TIMESTAMP_TOLERANCE = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class InvariantResult:
    name: str
    passed: bool
    message: str
    detail: dict[str, object] = field(default_factory=dict)
    #: A failing invariant is fatal unless it is advisory. Advisory failures
    #: route to human review instead of rejecting the link outright, which
    #: matters for rules like the settlement-lag bound where the real world is
    #: occasionally just slow.
    advisory: bool = False

    @property
    def blocks_automation(self) -> bool:
        return not self.passed


@dataclass(slots=True)
class AllocationProposal:
    """A proposed link, with the amount attributed from left to right."""

    left: SourceRecord
    right: SourceRecord
    relation: MatchRelation
    allocated_subunits: int

    @property
    def currency(self) -> str:
        return self.left.currency


@dataclass(slots=True)
class LedgerView:
    """Allocations already accepted, used for capacity and balance checks.

    Capacity is tracked per ``(record, relation)`` rather than per record,
    because a record's capacity is not a single pool. A payment being refunded
    and the same payment settling into a batch consume different, independent
    budgets; sharing one counter would make every refunded payment look
    over-allocated the moment it also settled.
    """

    #: (record id, relation) -> subunits already allocated out of that record
    allocated_out: dict[tuple[str, MatchRelation], int] = field(
        default_factory=lambda: defaultdict(int)
    )
    #: target record id -> list of (source record id, relation, subunits)
    allocated_in: dict[str, list[tuple[str, MatchRelation, int]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    #: pairs already linked, to make double-allocation detectable
    linked_pairs: set[tuple[str, str, MatchRelation]] = field(default_factory=set)

    def apply(self, proposal: AllocationProposal) -> None:
        self.allocated_out[(proposal.left.id, proposal.relation)] += abs(
            proposal.allocated_subunits
        )
        self.allocated_in[proposal.right.id].append(
            (proposal.left.id, proposal.relation, proposal.allocated_subunits)
        )
        self.linked_pairs.add((proposal.left.id, proposal.right.id, proposal.relation))

    def outbound(self, record_id: str, relation: MatchRelation) -> int:
        return self.allocated_out.get((record_id, relation), 0)

    def inbound(self, record_id: str) -> list[tuple[str, MatchRelation, int]]:
        return self.allocated_in.get(record_id, [])


# ---------------------------------------------------------------------------
# Pairwise invariants
# ---------------------------------------------------------------------------


def check_relation_endpoints(proposal: AllocationProposal) -> InvariantResult:
    expected = RELATION_ENDPOINTS.get(proposal.relation)
    actual = (proposal.left.record_kind, proposal.right.record_kind)
    passed = expected is not None and actual == expected
    return InvariantResult(
        name="relation_endpoints_valid",
        passed=passed,
        message=(
            "record kinds match the relation"
            if passed
            else f"{proposal.relation.value} cannot link {actual[0]} to {actual[1]}"
        ),
        detail={
            "expected": [k.value for k in expected] if expected else None,
            "actual": [k.value for k in actual],
        },
    )


def check_currency(proposal: AllocationProposal) -> InvariantResult:
    left, right = proposal.left, proposal.right
    passed = left.currency == right.currency
    return InvariantResult(
        name="currency_matches",
        passed=passed,
        message=(
            f"both records are {left.currency}"
            if passed
            else f"currency mismatch: {left.currency} cannot settle into {right.currency}"
        ),
        detail={"left_currency": left.currency, "right_currency": right.currency},
    )


def check_allocation_within_source(proposal: AllocationProposal) -> InvariantResult:
    """An allocation may not exceed the source record's own amount."""
    magnitude = abs(proposal.allocated_subunits)
    available = abs(proposal.left.amount_subunits)
    # Fee records allocate fee-plus-tax, which legitimately exceeds the fee
    # column alone, so the comparison uses the combined deduction.
    if proposal.left.record_kind == RecordKind.FEE:
        available = abs(proposal.left.amount_subunits) + abs(proposal.left.tax_subunits or 0)
    passed = magnitude <= available
    return InvariantResult(
        name="allocation_within_source_amount",
        passed=passed,
        message=(
            "allocated amount is within the source record"
            if passed
            else (
                f"allocated {Money(magnitude, proposal.currency)} exceeds the record's "
                f"{Money(available, proposal.currency)}"
            )
        ),
        detail={"allocated": magnitude, "available": available},
    )


def check_capacity(proposal: AllocationProposal, ledger: LedgerView) -> InvariantResult:
    """A record may not be allocated more than once in a one-to-one relation.

    Many-to-one relations legitimately fan in (many payments into one
    settlement), so capacity there is bounded by amount rather than by count.
    """
    if proposal.relation in MANY_TO_ONE_RELATIONS:
        already = ledger.outbound(proposal.left.id, proposal.relation)
        magnitude = abs(proposal.allocated_subunits)
        available = abs(proposal.left.amount_subunits) + abs(proposal.left.tax_subunits or 0)
        passed = already + magnitude <= available
        return InvariantResult(
            name="source_capacity_available",
            passed=passed,
            message=(
                "source record has remaining unallocated value"
                if passed
                else (
                    f"record already has {Money(already, proposal.currency)} allocated; "
                    f"adding {Money(magnitude, proposal.currency)} exceeds its value"
                )
            ),
            detail={"already_allocated": already, "requested": magnitude, "available": available},
        )

    already = ledger.outbound(proposal.left.id, proposal.relation)
    passed = already == 0
    return InvariantResult(
        name="source_not_already_allocated",
        passed=passed,
        message=(
            "source record is unallocated"
            if passed
            else "source record is already linked in a one-to-one relation"
        ),
        detail={"already_allocated": already},
    )


def check_not_duplicate_link(proposal: AllocationProposal, ledger: LedgerView) -> InvariantResult:
    key = (proposal.left.id, proposal.right.id, proposal.relation)
    passed = key not in ledger.linked_pairs
    return InvariantResult(
        name="link_not_duplicated",
        passed=passed,
        message="this pair is not already linked" if passed else "this pair is already linked",
    )


def check_date_ordering(proposal: AllocationProposal) -> InvariantResult:
    """Money cannot move before the event that caused it.

    The tolerance widens when either side is date-only. A settlement report
    giving ``25/07/2026`` is stored as midnight, so a payment captured at 11:30
    that day would look like it happened *after* its own settlement. Treating
    that as a violation would reject a correct link on the strength of a time
    the source never provided, so date-only comparisons are widened to a full
    day and reported as unasserted rather than passed.
    """
    left_at, right_at = proposal.left.occurred_at, proposal.right.occurred_at
    if left_at is None or right_at is None:
        return InvariantResult(
            name="date_ordering_possible",
            passed=True,
            message="timestamp missing on one side; ordering not asserted",
            detail={"asserted": False},
            advisory=True,
        )

    relation = proposal.relation
    delta = right_at - left_at
    date_only = bool(
        getattr(proposal.left, "timestamp_is_date_only", False)
        or getattr(proposal.right, "timestamp_is_date_only", False)
    )
    if date_only:
        # One side is accurate only to the day, so ordering within that day
        # cannot be established either way.
        tolerance = timedelta(days=1) + TIMESTAMP_TOLERANCE
        passed = delta >= -tolerance
        return InvariantResult(
            name="date_ordering_possible",
            passed=passed,
            message=(
                "ordering is consistent to day precision, which is all the source provides"
                if passed
                else f"target precedes source by {-delta}, beyond day-level ambiguity"
            ),
            detail={
                "delta_hours": round(delta.total_seconds() / 3600, 2),
                "day_precision_only": True,
            },
            advisory=True,
        )

    if relation in {
        MatchRelation.ORDER_TO_PAYMENT,
        MatchRelation.PAYMENT_TO_REFUND,
        MatchRelation.PAYMENT_TO_SETTLEMENT,
        MatchRelation.REFUND_TO_SETTLEMENT,
    }:
        passed = delta >= -TIMESTAMP_TOLERANCE
        return InvariantResult(
            name="date_ordering_possible",
            passed=passed,
            message=(
                "the later record does not precede the earlier one"
                if passed
                else f"target precedes source by {-delta}"
            ),
            detail={"delta_hours": round(delta.total_seconds() / 3600, 2)},
        )

    if relation is MatchRelation.SETTLEMENT_TO_BANK_CREDIT:
        early_limit = -timedelta(hours=BANK_CREDIT_EARLY_TOLERANCE_HOURS)
        passed = delta >= early_limit
        return InvariantResult(
            name="date_ordering_possible",
            passed=passed,
            message=(
                "bank credit does not precede its settlement"
                if passed
                else f"bank credit precedes the settlement by {-delta}"
            ),
            detail={"delta_hours": round(delta.total_seconds() / 3600, 2)},
        )

    return InvariantResult(
        name="date_ordering_possible", passed=True, message="no ordering constraint"
    )


def check_settlement_lag(proposal: AllocationProposal) -> InvariantResult:
    """Bound how long money may take to arrive. Advisory, not fatal."""
    left_at, right_at = proposal.left.occurred_at, proposal.right.occurred_at
    if left_at is None or right_at is None:
        return InvariantResult(
            name="lag_within_expected_window",
            passed=True,
            message="timestamp missing; lag not asserted",
            advisory=True,
        )
    lag_days = (right_at - left_at).total_seconds() / 86400
    if proposal.relation is MatchRelation.SETTLEMENT_TO_BANK_CREDIT:
        limit = MAX_BANK_CREDIT_LAG_DAYS
    elif proposal.relation in {
        MatchRelation.PAYMENT_TO_SETTLEMENT,
        MatchRelation.REFUND_TO_SETTLEMENT,
        MatchRelation.FEE_TO_SETTLEMENT,
    }:
        limit = MAX_SETTLEMENT_LAG_DAYS
    else:
        return InvariantResult(
            name="lag_within_expected_window", passed=True, message="no lag constraint"
        )
    passed = lag_days <= limit
    return InvariantResult(
        name="lag_within_expected_window",
        passed=passed,
        message=(
            f"lag of {lag_days:.1f} days is within the {limit}-day window"
            if passed
            else f"lag of {lag_days:.1f} days exceeds the {limit}-day window"
        ),
        detail={"lag_days": round(lag_days, 2), "limit_days": limit},
        advisory=True,
    )


def check_refund_not_over_payment(
    proposal: AllocationProposal, ledger: LedgerView
) -> InvariantResult:
    """A payment cannot be refunded for more than it captured."""
    if proposal.relation is not MatchRelation.PAYMENT_TO_REFUND:
        return InvariantResult(
            name="refund_within_payment", passed=True, message="not a refund link"
        )
    payment_amount = abs(proposal.left.amount_subunits)
    already_refunded = ledger.outbound(proposal.left.id, MatchRelation.PAYMENT_TO_REFUND)
    requested = abs(proposal.allocated_subunits)
    passed = already_refunded + requested <= payment_amount
    return InvariantResult(
        name="refund_within_payment",
        passed=passed,
        message=(
            "refund total does not exceed the captured payment"
            if passed
            else (
                f"refunds would total {Money(already_refunded + requested, proposal.currency)} "
                f"against a captured {Money(payment_amount, proposal.currency)}"
            )
        ),
        detail={
            "payment": payment_amount,
            "already_refunded": already_refunded,
            "requested": requested,
        },
    )


def check_unit_confusion(proposal: AllocationProposal) -> InvariantResult:
    """Flag a link whose two sides differ by exactly a factor of 100."""
    left = Money(proposal.left.amount_subunits, proposal.left.currency)
    right = Money(proposal.right.amount_subunits, proposal.right.currency)
    if left.currency != right.currency:
        return InvariantResult(
            name="no_unit_confusion", passed=True, message="currencies differ; check not applicable"
        )
    confused = looks_like_unit_confusion(left, right)
    return InvariantResult(
        name="no_unit_confusion",
        passed=not confused,
        message=(
            "amounts are not a 100x multiple of each other"
            if not confused
            else f"{left} and {right} differ by exactly 100x, suggesting a paise/rupee error"
        ),
        detail={"left": left.subunits, "right": right.subunits},
    )


PAIRWISE_INVARIANTS = (
    check_relation_endpoints,
    check_currency,
    check_allocation_within_source,
    check_date_ordering,
    check_settlement_lag,
    check_unit_confusion,
)

LEDGER_INVARIANTS = (
    check_capacity,
    check_not_duplicate_link,
    check_refund_not_over_payment,
)


def evaluate_pairwise(
    proposal: AllocationProposal, ledger: LedgerView | None = None
) -> list[InvariantResult]:
    """Run every applicable pairwise invariant against *proposal*."""
    results = [check(proposal) for check in PAIRWISE_INVARIANTS]
    if ledger is not None:
        results.extend(check(proposal, ledger) for check in LEDGER_INVARIANTS)
    return results


def blocking_failures(results: list[InvariantResult]) -> list[InvariantResult]:
    """Failures that must prevent automatic acceptance."""
    return [result for result in results if not result.passed and not result.advisory]


def advisory_failures(results: list[InvariantResult]) -> list[InvariantResult]:
    return [result for result in results if not result.passed and result.advisory]


# ---------------------------------------------------------------------------
# Aggregate invariants
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SettlementBalance:
    settlement_id: str
    currency: str
    reported_net: int
    allocated_total: int
    payment_component: int
    refund_component: int
    fee_component: int
    contributor_count: int

    @property
    def difference(self) -> int:
        return self.allocated_total - self.reported_net

    @property
    def balanced(self) -> bool:
        return self.difference == 0


def check_settlement_balance(
    settlement: SourceRecord,
    allocations: list[tuple[SourceRecord, MatchRelation, int]],
) -> tuple[InvariantResult, SettlementBalance]:
    """Assert a settlement's net equals the sum of what was allocated to it.

    This is the invariant the whole product rests on. Payments contribute their
    net of fee and tax, refunds contribute negatively, and the total must equal
    the settlement's reported net exactly — no tolerance, because both sides are
    integers and a tolerance would let real breaks hide inside it.
    """
    payment_component = 0
    refund_component = 0
    fee_component = 0
    total = 0
    for _record, relation, subunits in allocations:
        total += subunits
        if relation is MatchRelation.PAYMENT_TO_SETTLEMENT:
            payment_component += subunits
        elif relation is MatchRelation.REFUND_TO_SETTLEMENT:
            refund_component += subunits
        elif relation is MatchRelation.FEE_TO_SETTLEMENT:
            fee_component += subunits

    balance = SettlementBalance(
        settlement_id=settlement.id,
        currency=settlement.currency,
        reported_net=settlement.amount_subunits,
        allocated_total=total,
        payment_component=payment_component,
        refund_component=refund_component,
        fee_component=fee_component,
        contributor_count=len(allocations),
    )
    difference = balance.difference
    return (
        InvariantResult(
            name="settlement_balances",
            passed=balance.balanced,
            message=(
                "allocations sum exactly to the reported net settlement"
                if balance.balanced
                else (
                    f"allocations total {Money(total, settlement.currency)} against a reported "
                    f"net of {Money(settlement.amount_subunits, settlement.currency)}, "
                    f"a difference of {Money(difference, settlement.currency)}"
                )
            ),
            detail={
                "reported_net": balance.reported_net,
                "allocated_total": balance.allocated_total,
                "difference": difference,
                "contributors": balance.contributor_count,
            },
        ),
        balance,
    )


def check_bank_credit_exact(settlement: SourceRecord, bank_credit: SourceRecord) -> InvariantResult:
    """A bank credit must equal the settlement net exactly.

    Unlike the many-to-one settlement composition, this is a one-to-one
    relationship with no fees in between, so any difference at all is a real
    break rather than an accounting nuance.
    """
    passed = (
        settlement.amount_subunits == bank_credit.amount_subunits
        and settlement.currency == bank_credit.currency
    )
    return InvariantResult(
        name="bank_credit_equals_settlement_net",
        passed=passed,
        message=(
            "bank credit equals the settlement net exactly"
            if passed
            else (
                f"bank credit {Money(bank_credit.amount_subunits, bank_credit.currency)} "
                f"does not equal settlement net "
                f"{Money(settlement.amount_subunits, settlement.currency)}"
            )
        ),
        detail={
            "settlement_net": settlement.amount_subunits,
            "bank_credit": bank_credit.amount_subunits,
        },
    )
