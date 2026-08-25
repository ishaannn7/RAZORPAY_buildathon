"""Accounting invariants.

These tests encode the rules a reviewer would insist on. The settlement-balance
property test is the important one: it asserts that no set of allocations can be
accepted unless it sums exactly to the reported net, which is the guarantee the
whole automatic path rests on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from hypothesis import given
from hypothesis import strategies as st

from reconproof.accounting import invariants as inv
from reconproof.domain.entities import MatchRelation, PaymentMethod, RecordKind, RecordStatus

BASE = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


def make_record(
    *,
    record_id: str = "r1",
    kind: RecordKind = RecordKind.PAYMENT,
    amount: int = 100_00,
    currency: str = "INR",
    occurred_at: datetime | None = BASE,
    fee: int | None = None,
    tax: int | None = None,
    date_only: bool = False,
) -> SimpleNamespace:
    """A stand-in for a persisted record.

    The invariants read a narrow set of attributes, so a lightweight object keeps
    these tests independent of the database and fast enough to use with
    Hypothesis.
    """
    return SimpleNamespace(
        id=record_id,
        record_kind=kind,
        amount_subunits=amount,
        currency=currency,
        occurred_at=occurred_at,
        fee_subunits=fee,
        tax_subunits=tax,
        timestamp_is_date_only=date_only,
        payment_method=PaymentMethod.UNKNOWN,
        status=RecordStatus.UNKNOWN,
        settlement_ref=None,
        payment_ref=None,
        order_ref=None,
        bank_ref=None,
        external_id=record_id,
        refund_total_subunits=None,
    )


def proposal(
    left: SimpleNamespace,
    right: SimpleNamespace,
    relation: MatchRelation,
    allocated: int | None = None,
) -> inv.AllocationProposal:
    return inv.AllocationProposal(
        left=left,  # type: ignore[arg-type]
        right=right,  # type: ignore[arg-type]
        relation=relation,
        allocated_subunits=allocated if allocated is not None else left.amount_subunits,
    )


class TestCurrency:
    def test_same_currency_passes(self) -> None:
        result = inv.check_currency(
            proposal(make_record(), make_record(record_id="r2"), MatchRelation.ORDER_TO_PAYMENT)
        )
        assert result.passed

    def test_cross_currency_blocks(self) -> None:
        left = make_record(currency="USD")
        right = make_record(record_id="r2", currency="INR")
        result = inv.check_currency(proposal(left, right, MatchRelation.ORDER_TO_PAYMENT))
        assert not result.passed
        assert not result.advisory  # fatal, not advisory


class TestDateOrdering:
    def test_target_after_source_passes(self) -> None:
        left = make_record(occurred_at=BASE)
        right = make_record(record_id="r2", occurred_at=BASE + timedelta(days=2))
        assert inv.check_date_ordering(
            proposal(left, right, MatchRelation.PAYMENT_TO_SETTLEMENT)
        ).passed

    def test_target_well_before_source_blocks(self) -> None:
        left = make_record(occurred_at=BASE)
        right = make_record(record_id="r2", occurred_at=BASE - timedelta(days=3))
        result = inv.check_date_ordering(proposal(left, right, MatchRelation.PAYMENT_TO_SETTLEMENT))
        assert not result.passed

    def test_date_only_widens_tolerance(self) -> None:
        # A settlement given as "25/07/2026" lands on midnight. A payment
        # captured at 11:30 the same day would look later than its own
        # settlement, which the source never actually said.
        left = make_record(occurred_at=BASE.replace(hour=11, minute=30))
        right = make_record(
            record_id="r2", occurred_at=BASE.replace(hour=0, minute=0), date_only=True
        )
        result = inv.check_date_ordering(proposal(left, right, MatchRelation.PAYMENT_TO_SETTLEMENT))
        assert result.passed
        assert result.detail["day_precision_only"] is True

    def test_missing_timestamp_does_not_assert(self) -> None:
        left = make_record(occurred_at=None)
        right = make_record(record_id="r2")
        result = inv.check_date_ordering(proposal(left, right, MatchRelation.PAYMENT_TO_SETTLEMENT))
        assert result.passed
        assert result.advisory


class TestCapacity:
    def test_capacity_is_tracked_per_relation(self) -> None:
        """A refunded payment must still be able to settle.

        Both facts are true of the same payment at once, and pooling their
        capacity would make the second one impossible.
        """
        payment = make_record(kind=RecordKind.PAYMENT, amount=100_00)
        refund = make_record(record_id="rf", kind=RecordKind.REFUND, amount=40_00)
        settlement = make_record(record_id="st", kind=RecordKind.SETTLEMENT, amount=500_00)
        ledger = inv.LedgerView()

        refund_link = proposal(payment, refund, MatchRelation.PAYMENT_TO_REFUND, 40_00)
        assert inv.check_capacity(refund_link, ledger).passed
        ledger.apply(refund_link)

        settle_link = proposal(payment, settlement, MatchRelation.PAYMENT_TO_SETTLEMENT, 100_00)
        assert inv.check_capacity(settle_link, ledger).passed

    def test_one_to_one_rejects_second_claim(self) -> None:
        settlement = make_record(record_id="st", kind=RecordKind.SETTLEMENT, amount=500_00)
        credit_a = make_record(record_id="c1", kind=RecordKind.BANK_CREDIT, amount=500_00)
        credit_b = make_record(record_id="c2", kind=RecordKind.BANK_CREDIT, amount=500_00)
        ledger = inv.LedgerView()

        first = proposal(settlement, credit_a, MatchRelation.SETTLEMENT_TO_BANK_CREDIT)
        ledger.apply(first)
        second = proposal(settlement, credit_b, MatchRelation.SETTLEMENT_TO_BANK_CREDIT)
        assert not inv.check_capacity(second, ledger).passed


class TestRefundLimits:
    def test_refund_within_payment_passes(self) -> None:
        payment = make_record(amount=100_00)
        refund = make_record(record_id="rf", kind=RecordKind.REFUND, amount=40_00)
        result = inv.check_refund_not_over_payment(
            proposal(payment, refund, MatchRelation.PAYMENT_TO_REFUND, 40_00), inv.LedgerView()
        )
        assert result.passed

    def test_over_refund_blocks(self) -> None:
        payment = make_record(amount=100_00)
        refund = make_record(record_id="rf", kind=RecordKind.REFUND, amount=140_00)
        result = inv.check_refund_not_over_payment(
            proposal(payment, refund, MatchRelation.PAYMENT_TO_REFUND, 140_00), inv.LedgerView()
        )
        assert not result.passed

    def test_cumulative_partial_refunds_block(self) -> None:
        payment = make_record(amount=100_00)
        ledger = inv.LedgerView()
        for index, amount in enumerate((60_00, 30_00)):
            refund = make_record(record_id=f"rf{index}", kind=RecordKind.REFUND, amount=amount)
            link = proposal(payment, refund, MatchRelation.PAYMENT_TO_REFUND, amount)
            assert inv.check_refund_not_over_payment(link, ledger).passed
            ledger.apply(link)
        # 60 + 30 + 20 = 110 against a captured 100.
        third = make_record(record_id="rf9", kind=RecordKind.REFUND, amount=20_00)
        assert not inv.check_refund_not_over_payment(
            proposal(payment, third, MatchRelation.PAYMENT_TO_REFUND, 20_00), ledger
        ).passed


class TestRelationEndpoints:
    def test_wrong_kinds_block(self) -> None:
        order = make_record(kind=RecordKind.ORDER)
        credit = make_record(record_id="c", kind=RecordKind.BANK_CREDIT)
        assert not inv.check_relation_endpoints(
            proposal(order, credit, MatchRelation.ORDER_TO_PAYMENT)
        ).passed


class TestSettlementBalance:
    def test_exact_balance_passes(self) -> None:
        settlement = make_record(record_id="st", kind=RecordKind.SETTLEMENT, amount=900_00)
        allocations = [
            (make_record(record_id="p1"), MatchRelation.PAYMENT_TO_SETTLEMENT, 1000_00),
            (
                make_record(record_id="f1", kind=RecordKind.FEE),
                MatchRelation.FEE_TO_SETTLEMENT,
                -60_00,
            ),
            (
                make_record(record_id="r1", kind=RecordKind.REFUND),
                MatchRelation.REFUND_TO_SETTLEMENT,
                -40_00,
            ),
        ]
        result, balance = inv.check_settlement_balance(settlement, allocations)  # type: ignore[arg-type]
        assert result.passed
        assert balance.balanced
        assert balance.difference == 0

    def test_one_paisa_short_fails(self) -> None:
        """No tolerance. A single paisa of slack would let real breaks hide."""
        settlement = make_record(record_id="st", kind=RecordKind.SETTLEMENT, amount=900_00)
        allocations = [
            (make_record(record_id="p1"), MatchRelation.PAYMENT_TO_SETTLEMENT, 899_99),
        ]
        result, balance = inv.check_settlement_balance(settlement, allocations)  # type: ignore[arg-type]
        assert not result.passed
        assert balance.difference == -1

    @given(
        payments=st.lists(st.integers(min_value=1, max_value=10**7), min_size=1, max_size=25),
        fee_bps=st.integers(min_value=0, max_value=500),
        refunds=st.lists(st.integers(min_value=0, max_value=10**6), max_size=6),
    )
    def test_constructed_balance_always_holds(
        self, payments: list[int], fee_bps: int, refunds: list[int]
    ) -> None:
        """Any correctly constructed settlement balances, at any scale.

        Built the way the pipeline builds one: gross from payments, fees
        deducted, refunds deducted. If integer arithmetic ever failed to close,
        this is where it would show.
        """
        gross = sum(payments)
        fees = sum(amount * fee_bps // 10_000 for amount in payments)
        refund_total = sum(refunds)
        net = gross - fees - refund_total

        settlement = make_record(record_id="st", kind=RecordKind.SETTLEMENT, amount=net)
        allocations: list[tuple[SimpleNamespace, MatchRelation, int]] = [
            (make_record(record_id=f"p{i}"), MatchRelation.PAYMENT_TO_SETTLEMENT, amount)
            for i, amount in enumerate(payments)
        ]
        if fees:
            allocations.append(
                (
                    make_record(record_id="fee", kind=RecordKind.FEE),
                    MatchRelation.FEE_TO_SETTLEMENT,
                    -fees,
                )
            )
        for index, amount in enumerate(refunds):
            if amount:
                allocations.append(
                    (
                        make_record(record_id=f"rf{index}", kind=RecordKind.REFUND),
                        MatchRelation.REFUND_TO_SETTLEMENT,
                        -amount,
                    )
                )

        result, balance = inv.check_settlement_balance(settlement, allocations)  # type: ignore[arg-type]
        assert result.passed, result.message
        assert balance.difference == 0


class TestBankCredit:
    def test_exact_match_required(self) -> None:
        settlement = make_record(record_id="st", kind=RecordKind.SETTLEMENT, amount=900_00)
        credit = make_record(record_id="c", kind=RecordKind.BANK_CREDIT, amount=900_00)
        assert inv.check_bank_credit_exact(settlement, credit).passed  # type: ignore[arg-type]

    def test_any_difference_fails(self) -> None:
        settlement = make_record(record_id="st", kind=RecordKind.SETTLEMENT, amount=900_00)
        credit = make_record(record_id="c", kind=RecordKind.BANK_CREDIT, amount=899_99)
        assert not inv.check_bank_credit_exact(settlement, credit).passed  # type: ignore[arg-type]


class TestUnitConfusionInvariant:
    def test_hundred_multiple_blocks(self) -> None:
        left = make_record(amount=500_000)
        right = make_record(record_id="r2", amount=5_000)
        assert not inv.check_unit_confusion(
            proposal(left, right, MatchRelation.ORDER_TO_PAYMENT)
        ).passed


class TestBlockingClassification:
    def test_advisory_failures_are_separated(self) -> None:
        """An advisory failure routes to review; a fatal one rejects outright."""
        results = [
            inv.InvariantResult(name="fatal", passed=False, message="", advisory=False),
            inv.InvariantResult(name="soft", passed=False, message="", advisory=True),
            inv.InvariantResult(name="fine", passed=True, message=""),
        ]
        assert [r.name for r in inv.blocking_failures(results)] == ["fatal"]
        assert [r.name for r in inv.advisory_failures(results)] == ["soft"]
