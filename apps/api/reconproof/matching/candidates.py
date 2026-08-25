"""Candidate generation for records that deterministic matching could not link.

This stage optimizes for recall: a true link that never becomes a candidate can
never be found, no matter how good the model is. Precision is imposed later by
the calibrated score, the conformal risk bound and the invariants.

Blocking keys keep the work near-linear. Comparing every unmatched bank credit
against every settlement would be quadratic, so candidates are drawn from an
amount-and-date index rather than from the full cross product.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import TYPE_CHECKING

from reconproof.domain.entities import MatchMethod, MatchRelation, RecordKind
from reconproof.domain.money import Money
from reconproof.matching.features import extract_features
from reconproof.matching.subsetsum import SubsetOutcome, attribute_exact_subset
from reconproof.matching.types import EvidenceDraft, ProposedLink

if TYPE_CHECKING:
    from reconproof.db.models import SourceRecord


def _bucket_days(moment: object, width: int = 1) -> int:
    from datetime import datetime

    if not isinstance(moment, datetime):
        return 0
    return int(moment.timestamp() // (86400 * width))


class CandidateGenerator:
    def __init__(
        self,
        *,
        window_days: int = 10,
        amount_tolerance_bps: int = 500,
        max_candidates_per_record: int = 25,
    ) -> None:
        self.window_days = window_days
        self.amount_tolerance_bps = amount_tolerance_bps
        self.max_candidates_per_record = max_candidates_per_record

    # -- blocking ----------------------------------------------------------

    def _amount_index(self, records: list[SourceRecord]) -> dict[int, list[SourceRecord]]:
        index: dict[int, list[SourceRecord]] = defaultdict(list)
        for record in records:
            index[abs(record.amount_subunits)].append(record)
        return index

    def _tolerance_band(self, amount: int) -> range:
        """Not used for exact-amount relations; kept for tolerance-based ones."""
        slack = max(1, amount * self.amount_tolerance_bps // 10_000)
        return range(amount - slack, amount + slack + 1)

    # -- relation-specific generation --------------------------------------

    def settlement_to_bank_credit(
        self,
        settlements: list[SourceRecord],
        unmatched_credits: list[SourceRecord],
        matched_settlement_ids: set[str],
    ) -> list[ProposedLink]:
        """Link an unmatched bank credit to the settlement it pays.

        A credit must equal a settlement's net *exactly*, so the amount is used
        as a blocking key rather than a scored feature. That makes the hard
        negatives in the dataset genuinely hard: they carry a real settlement's
        amount, so only reference and date evidence can separate them.
        """
        available = [s for s in settlements if s.id not in matched_settlement_ids]
        index = self._amount_index(available)
        links: list[ProposedLink] = []

        for credit in unmatched_credits:
            pool = index.get(abs(credit.amount_subunits), [])
            if not pool:
                continue
            in_window = [
                settlement
                for settlement in pool
                if self._within_window(settlement, credit, self.window_days)
            ]
            if not in_window:
                continue
            competing = len(in_window)
            ranked = sorted(in_window, key=lambda s: self._absolute_gap(s, credit))
            for rank, settlement in enumerate(ranked[: self.max_candidates_per_record]):
                links.append(
                    self._build(
                        left=settlement,
                        right=credit,
                        relation=MatchRelation.SETTLEMENT_TO_BANK_CREDIT,
                        allocated_subunits=settlement.amount_subunits,
                        generator="candidate:amount_window",
                        competing=competing,
                        rank=rank,
                    )
                )
        return links

    def order_to_payment(
        self,
        orders: list[SourceRecord],
        unmatched_payments: list[SourceRecord],
        matched_order_ids: set[str],
    ) -> list[ProposedLink]:
        """Link a payment whose order reference is missing back to its order."""
        available = [order for order in orders if order.id not in matched_order_ids]
        index = self._amount_index(available)
        links: list[ProposedLink] = []

        for payment in unmatched_payments:
            pool = index.get(abs(payment.amount_subunits), [])
            # An order precedes its payment, usually within hours. A tight
            # window here is what keeps same-amount orders from all becoming
            # candidates.
            in_window = [
                order
                for order in pool
                if self._within_window(order, payment, 2)
                and (
                    order.occurred_at is None
                    or payment.occurred_at is None
                    or order.occurred_at <= payment.occurred_at + timedelta(hours=6)
                )
            ]
            if not in_window:
                continue
            competing = len(in_window)
            ranked = sorted(in_window, key=lambda o: self._absolute_gap(o, payment))
            for rank, order in enumerate(ranked[: self.max_candidates_per_record]):
                links.append(
                    self._build(
                        left=order,
                        right=payment,
                        relation=MatchRelation.ORDER_TO_PAYMENT,
                        allocated_subunits=payment.amount_subunits,
                        generator="candidate:order_amount_window",
                        competing=competing,
                        rank=rank,
                    )
                )
        return links

    def payment_to_settlement(
        self,
        settlements: list[SourceRecord],
        unmatched_payments: list[SourceRecord],
        residual_by_settlement: dict[str, int],
    ) -> list[ProposedLink]:
        """Link a payment to a settlement batch using the batch's residual.

        A settlement whose allocations already balance has no room for another
        payment. Only batches with an unexplained remainder large enough to
        absorb this payment are considered, which is both a strong filter and
        the financially correct reason to consider them at all.
        """
        links: list[ProposedLink] = []
        for payment in unmatched_payments:
            gross = abs(payment.amount_subunits)
            eligible = [
                settlement
                for settlement in settlements
                if residual_by_settlement.get(settlement.id, 0) >= gross
                and self._within_window(payment, settlement, self.window_days)
            ]
            if not eligible:
                continue
            competing = len(eligible)
            ranked = sorted(eligible, key=lambda s: self._absolute_gap(payment, s))
            for rank, settlement in enumerate(ranked[: self.max_candidates_per_record]):
                links.append(
                    self._build(
                        left=payment,
                        right=settlement,
                        relation=MatchRelation.PAYMENT_TO_SETTLEMENT,
                        allocated_subunits=payment.amount_subunits,
                        generator="candidate:settlement_residual",
                        competing=competing,
                        rank=rank,
                    )
                )
        return links

    def payment_to_refund(
        self,
        payments: list[SourceRecord],
        unmatched_refunds: list[SourceRecord],
    ) -> list[ProposedLink]:
        """Link a refund with no payment reference to a plausible payment.

        A refund may be partial, so the amount is not a blocking key here: any
        payment at least as large as the refund is eligible. That is a much
        looser filter, which is precisely why these cases so often end up in
        review rather than auto-accepted.
        """
        links: list[ProposedLink] = []
        for refund in unmatched_refunds:
            magnitude = abs(refund.amount_subunits)
            eligible = [
                payment
                for payment in payments
                if abs(payment.amount_subunits) >= magnitude
                and self._within_window(payment, refund, 30)
            ]
            if not eligible:
                continue
            competing = len(eligible)
            ranked = sorted(eligible, key=lambda p: self._absolute_gap(p, refund))
            for rank, payment in enumerate(ranked[: self.max_candidates_per_record]):
                links.append(
                    self._build(
                        left=payment,
                        right=refund,
                        relation=MatchRelation.PAYMENT_TO_REFUND,
                        allocated_subunits=refund.amount_subunits,
                        generator="candidate:refund_amount_window",
                        competing=competing,
                        rank=rank,
                    )
                )
        return links

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _within_window(left: SourceRecord, right: SourceRecord, days: float) -> bool:
        if left.occurred_at is None or right.occurred_at is None:
            # A missing timestamp cannot exclude a candidate; it only makes the
            # candidate weaker, which the features already express.
            return True
        return abs((right.occurred_at - left.occurred_at).total_seconds()) <= days * 86400

    @staticmethod
    def _not_before(earlier: SourceRecord, later: SourceRecord) -> bool:
        """True when *later* does not precede *earlier* beyond day ambiguity."""
        if earlier.occurred_at is None or later.occurred_at is None:
            return True
        slack = (
            timedelta(days=1)
            if (earlier.timestamp_is_date_only or later.timestamp_is_date_only)
            else timedelta(hours=6)
        )
        return later.occurred_at >= earlier.occurred_at - slack

    @staticmethod
    def _absolute_gap(left: SourceRecord, right: SourceRecord) -> float:
        if left.occurred_at is None or right.occurred_at is None:
            return float("inf")
        return abs((right.occurred_at - left.occurred_at).total_seconds())

    def _build(
        self,
        *,
        left: SourceRecord,
        right: SourceRecord,
        relation: MatchRelation,
        allocated_subunits: int,
        generator: str,
        competing: int,
        rank: int,
    ) -> ProposedLink:
        features = extract_features(
            left,
            right,
            relation,
            window_days=self.window_days,
            competing_candidates=competing,
            amount_rank=rank,
        )
        return ProposedLink(
            left=left,
            right=right,
            relation=relation,
            allocated_subunits=allocated_subunits,
            method=MatchMethod.CALIBRATED_MODEL,
            generator=generator,
            features=features,
            evidence=list(_evidence_from_features(features)),
        )


def _evidence_from_features(features: dict[str, float]) -> list[EvidenceDraft]:
    """Turn the discriminating features into citable evidence.

    Only features that actually argue for or against the link are recorded, and
    contrary evidence is kept with ``supports=False`` so a reviewer sees both
    sides of the case.
    """
    from reconproof.matching.features import describe_feature

    interesting = (
        "amount_exact",
        "reference_exact",
        "reference_containment",
        "reference_tail_match",
        "within_window",
        "is_sole_candidate",
        "amount_unit_confusion",
        "currency_match",
    )
    drafts: list[EvidenceDraft] = []
    for name in interesting:
        value = features.get(name, 0.0)
        supports = value == 0.0 if name == "amount_unit_confusion" else value > 0.0
        # A neutral feature is not evidence; skip it rather than pad the list.
        if (
            name not in {"amount_unit_confusion"}
            and value == 0.0
            and name
            in {
                "reference_tail_match",
                "reference_exact",
            }
        ):
            continue
        drafts.append(
            EvidenceDraft(
                kind=name,
                statement=describe_feature(name, value),
                supports=supports,
                weight=abs(value),
                detail={"feature": name, "value": value},
            )
        )
    drafts.append(
        EvidenceDraft(
            kind="day_delta_abs",
            statement=describe_feature("day_delta_abs", features.get("day_delta_abs", 0.0)),
            supports=features.get("within_window", 0.0) > 0,
            weight=features.get("day_delta_abs", 0.0),
            detail={"feature": "day_delta_abs", "value": features.get("day_delta_abs", 0.0)},
        )
    )
    return drafts


def record_kinds(records: list[SourceRecord]) -> dict[RecordKind, list[SourceRecord]]:
    grouped: dict[RecordKind, list[SourceRecord]] = defaultdict(list)
    for record in records:
        grouped[record.record_kind].append(record)
    return grouped


def generate_all_candidates(
    records: list[SourceRecord],
    exact_links: list[ProposedLink],
    *,
    window_days: int,
    amount_tolerance_bps: int,
    max_candidates_per_record: int,
) -> list[ProposedLink]:
    """Produce the complete candidate set for a batch.

    Both the live pipeline and the training harness call this. Keeping it in one
    place is not tidiness: if the two diverged, the measured precision would
    describe a candidate set that never actually runs, and every number in the
    evaluation report would be quietly wrong.
    """
    grouped = record_kinds(records)
    generator = CandidateGenerator(
        window_days=window_days,
        amount_tolerance_bps=amount_tolerance_bps,
        max_candidates_per_record=max_candidates_per_record,
    )

    matched_left: dict[MatchRelation, set[str]] = defaultdict(set)
    matched_right: dict[MatchRelation, set[str]] = defaultdict(set)
    allocated: dict[str, int] = defaultdict(int)
    refund_allocated: dict[str, int] = defaultdict(int)

    for link in exact_links:
        matched_left[link.relation].add(link.left.id)
        matched_right[link.relation].add(link.right.id)
        if link.right.record_kind is RecordKind.SETTLEMENT:
            allocated[link.right.id] += link.allocated_subunits
            if link.relation is MatchRelation.REFUND_TO_SETTLEMENT:
                refund_allocated[link.right.id] += abs(link.allocated_subunits)

    settlements = grouped.get(RecordKind.SETTLEMENT, [])
    residual = {
        settlement.id: settlement.amount_subunits - allocated.get(settlement.id, 0)
        for settlement in settlements
    }
    refund_residual = {
        settlement.id: max(
            0,
            abs(settlement.refund_total_subunits or 0) - refund_allocated.get(settlement.id, 0),
        )
        for settlement in settlements
    }

    def pending(kind: RecordKind, relation: MatchRelation, side: str) -> list[SourceRecord]:
        claimed = matched_left[relation] if side == "left" else matched_right[relation]
        return [record for record in grouped.get(kind, []) if record.id not in claimed]

    links: list[ProposedLink] = []
    links.extend(
        generator.settlement_to_bank_credit(
            settlements,
            pending(RecordKind.BANK_CREDIT, MatchRelation.SETTLEMENT_TO_BANK_CREDIT, "right"),
            matched_left[MatchRelation.SETTLEMENT_TO_BANK_CREDIT],
        )
    )
    links.extend(
        generator.order_to_payment(
            grouped.get(RecordKind.ORDER, []),
            pending(RecordKind.PAYMENT, MatchRelation.ORDER_TO_PAYMENT, "right"),
            matched_left[MatchRelation.ORDER_TO_PAYMENT],
        )
    )
    links.extend(
        generator.payment_to_settlement(
            settlements,
            pending(RecordKind.PAYMENT, MatchRelation.PAYMENT_TO_SETTLEMENT, "left"),
            residual,
        )
    )
    links.extend(
        generator.payment_to_refund(
            grouped.get(RecordKind.PAYMENT, []),
            pending(RecordKind.REFUND, MatchRelation.PAYMENT_TO_REFUND, "right"),
        )
    )
    # Refund attribution is resolved by exact subset-sum against each
    # settlement's reported refund total, not by scoring pairs. Only the
    # genuinely ambiguous remainder becomes scored candidates.
    determined, ambiguous_refunds = attribute_refunds(
        settlements,
        pending(RecordKind.REFUND, MatchRelation.REFUND_TO_SETTLEMENT, "left"),
        refund_residual,
        window_days=window_days,
    )
    links.extend(determined)
    links.extend(ambiguous_refunds)
    return links


def attribute_refunds(
    settlements: list[SourceRecord],
    unmatched_refunds: list[SourceRecord],
    refund_residual: dict[str, int],
    *,
    window_days: int,
) -> tuple[list[ProposedLink], list[ProposedLink]]:
    """Attribute refunds to settlements by exact subset-sum.

    Returns ``(determined, ambiguous)``. A determined link is proven by
    arithmetic rather than estimated, so it carries the composite-exact method
    and is eligible for automatic acceptance. An ambiguous one names the
    competing possibilities and goes to a human.

    Settlements are processed from the most constrained upward: a batch with a
    single plausible refund should claim it before a batch with twenty options
    does, otherwise an early greedy choice can make a later certainty
    impossible.
    """
    determined: list[ProposedLink] = []
    ambiguous: list[ProposedLink] = []
    claimed: set[str] = set()

    eligible_by_settlement: dict[str, list[SourceRecord]] = {}
    for settlement in settlements:
        target = refund_residual.get(settlement.id, 0)
        if target <= 0:
            continue
        eligible_by_settlement[settlement.id] = [
            refund
            for refund in unmatched_refunds
            if CandidateGenerator._not_before(refund, settlement)
            and CandidateGenerator._within_window(refund, settlement, window_days)
            and abs(refund.amount_subunits) <= target
        ]

    by_id = {settlement.id: settlement for settlement in settlements}
    ordered = sorted(eligible_by_settlement, key=lambda key: len(eligible_by_settlement[key]))

    for settlement_id in ordered:
        settlement = by_id[settlement_id]
        target = refund_residual.get(settlement_id, 0)
        pool = [
            refund for refund in eligible_by_settlement[settlement_id] if refund.id not in claimed
        ]
        if not pool:
            continue
        solution = attribute_exact_subset(
            [(refund.id, abs(refund.amount_subunits)) for refund in pool], target
        )
        pool_by_id = {refund.id: refund for refund in pool}

        if solution.outcome is SubsetOutcome.UNIQUE and solution.unique_solution:
            members = [pool_by_id[identifier] for identifier in solution.unique_solution]
            for refund in members:
                claimed.add(refund.id)
                determined.append(
                    _refund_link(
                        refund,
                        settlement,
                        window_days=window_days,
                        method=MatchMethod.EXACT_COMPOSITE,
                        score=1.0,
                        risk=0.0,
                        generator="subsetsum:unique",
                        statement=(
                            f"This refund is part of the only combination of {len(members)} "
                            f"refund(s) that sums exactly to the settlement's reported refund "
                            f"total of {Money(target, settlement.currency)}."
                        ),
                        supports=True,
                    )
                )
        elif solution.outcome is SubsetOutcome.AMBIGUOUS:
            for identifier in solution.involved_ids:
                refund = pool_by_id[identifier]
                ambiguous.append(
                    _refund_link(
                        refund,
                        settlement,
                        window_days=window_days,
                        method=MatchMethod.CALIBRATED_MODEL,
                        score=None,
                        risk=None,
                        generator="subsetsum:ambiguous",
                        statement=(
                            f"{len(solution.solutions)} different combinations of refunds sum "
                            f"to this settlement's refund total of "
                            f"{Money(target, settlement.currency)}, so the attribution is not "
                            "determined by the data."
                        ),
                        supports=False,
                    )
                )
        # NONE and SKIPPED_TOO_LARGE deliberately produce no link: the refund
        # total stays unexplained and is accounted for as such.

    return determined, ambiguous


def _refund_link(
    refund: SourceRecord,
    settlement: SourceRecord,
    *,
    window_days: int,
    method: MatchMethod,
    score: float | None,
    risk: float | None,
    generator: str,
    statement: str,
    supports: bool,
) -> ProposedLink:
    """Build a refund link whose outcome the subset-sum stage already settled."""
    features = extract_features(
        refund,
        settlement,
        MatchRelation.REFUND_TO_SETTLEMENT,
        window_days=window_days,
        competing_candidates=1 if supports else 2,
    )
    return ProposedLink(
        left=refund,
        right=settlement,
        relation=MatchRelation.REFUND_TO_SETTLEMENT,
        allocated_subunits=-abs(refund.amount_subunits),
        method=method,
        generator=generator,
        features=features,
        score=score,
        risk=risk,
        scoring_final=True,
        evidence=[
            EvidenceDraft(
                kind="subset_sum_attribution",
                statement=statement,
                supports=supports,
                weight=1.0 if supports else 0.0,
            )
        ],
    )
