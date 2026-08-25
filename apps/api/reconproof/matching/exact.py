"""Deterministic matching on shared identifiers.

This stage runs first and does the bulk of the work. It is also the baseline the
calibrated model has to beat: reporting a model's precision without showing what
plain reference matching already achieves would overstate its contribution.

Nothing here is statistical. A link is proposed only when two records carry the
same identifier, or when a chain of such identifiers implies the link.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from reconproof.domain.entities import MatchMethod, MatchRelation, RecordKind
from reconproof.ingest.keys import normalize_reference
from reconproof.matching.types import EvidenceDraft, ProposedLink

if TYPE_CHECKING:
    from reconproof.db.models import SourceRecord


def _index_by(records: list[SourceRecord], attribute: str) -> dict[str, list[SourceRecord]]:
    """Group records by a normalized reference attribute.

    Values map to a *list* rather than a single record because a reference is
    not guaranteed unique in real data. An ambiguous reference must stay
    ambiguous, not be silently resolved to whichever row was seen last.
    """
    index: dict[str, list[SourceRecord]] = defaultdict(list)
    for record in records:
        value = getattr(record, attribute, None)
        key = normalize_reference(value)
        if key:
            index[key].append(record)
    return index


def _by_kind(records: list[SourceRecord]) -> dict[RecordKind, list[SourceRecord]]:
    grouped: dict[RecordKind, list[SourceRecord]] = defaultdict(list)
    for record in records:
        grouped[record.record_kind].append(record)
    return grouped


def _net_of_fees(payment: SourceRecord) -> int:
    return payment.amount_subunits - (payment.fee_subunits or 0) - (payment.tax_subunits or 0)


def match_exact(records: list[SourceRecord]) -> tuple[list[ProposedLink], set[str]]:
    """Propose every link implied by shared identifiers.

    Returns the proposals and the set of reference keys that were ambiguous, so
    the candidate stage knows which records still need probabilistic treatment
    despite appearing to have a usable reference.
    """
    by_kind = _by_kind(records)
    orders = by_kind.get(RecordKind.ORDER, [])
    payments = by_kind.get(RecordKind.PAYMENT, [])
    refunds = by_kind.get(RecordKind.REFUND, [])
    settlements = by_kind.get(RecordKind.SETTLEMENT, [])
    bank_credits = by_kind.get(RecordKind.BANK_CREDIT, [])
    fees = by_kind.get(RecordKind.FEE, [])

    links: list[ProposedLink] = []
    ambiguous: set[str] = set()

    order_index = _index_by(orders, "order_ref")
    payment_index = _index_by(payments, "payment_ref")
    settlement_index = _index_by(settlements, "settlement_ref")

    # -- order <- payment --------------------------------------------------
    for payment in payments:
        key = normalize_reference(payment.order_ref)
        if not key:
            continue
        matches = order_index.get(key, [])
        if len(matches) != 1:
            if matches:
                ambiguous.add(key)
            continue
        order = matches[0]
        links.append(
            ProposedLink(
                left=order,
                right=payment,
                relation=MatchRelation.ORDER_TO_PAYMENT,
                allocated_subunits=payment.amount_subunits,
                method=MatchMethod.EXACT_REFERENCE,
                generator="exact:order_ref",
                score=1.0,
                risk=0.0,
                evidence=[
                    EvidenceDraft(
                        kind="shared_order_reference",
                        statement=(
                            f"Payment {payment.payment_ref} names order {order.order_ref} directly."
                        ),
                        weight=1.0,
                        detail={"order_ref": order.order_ref},
                    )
                ],
            )
        )

    # -- payment <- refund -------------------------------------------------
    for refund in refunds:
        key = normalize_reference(refund.payment_ref)
        if not key:
            continue
        matches = payment_index.get(key, [])
        if len(matches) != 1:
            if matches:
                ambiguous.add(key)
            continue
        payment = matches[0]
        links.append(
            ProposedLink(
                left=payment,
                right=refund,
                relation=MatchRelation.PAYMENT_TO_REFUND,
                allocated_subunits=refund.amount_subunits,
                method=MatchMethod.EXACT_REFERENCE,
                generator="exact:payment_ref",
                score=1.0,
                risk=0.0,
                evidence=[
                    EvidenceDraft(
                        kind="shared_payment_reference",
                        statement=(
                            f"Refund {refund.external_id} names payment "
                            f"{payment.payment_ref} directly."
                        ),
                        weight=1.0,
                        detail={"payment_ref": payment.payment_ref},
                    )
                ],
            )
        )

    # -- fee -> settlement, and the payment->settlement bridge it implies ---
    #
    # The fee report is the only source carrying both a payment id and a
    # settlement id, which makes it a deterministic bridge for a relationship
    # neither the payments nor the settlements file states on its own.
    payment_to_settlement: dict[str, SourceRecord] = {}
    for fee in fees:
        settlement_key = normalize_reference(fee.settlement_ref)
        if not settlement_key:
            continue
        matches = settlement_index.get(settlement_key, [])
        if len(matches) != 1:
            if matches:
                ambiguous.add(settlement_key)
            continue
        settlement = matches[0]
        deduction = -(abs(fee.amount_subunits) + abs(fee.tax_subunits or 0))
        links.append(
            ProposedLink(
                left=fee,
                right=settlement,
                relation=MatchRelation.FEE_TO_SETTLEMENT,
                allocated_subunits=deduction,
                method=MatchMethod.EXACT_REFERENCE,
                generator="exact:fee_settlement_ref",
                score=1.0,
                risk=0.0,
                evidence=[
                    EvidenceDraft(
                        kind="shared_settlement_reference",
                        statement=(
                            f"Fee record {fee.external_id} names settlement "
                            f"{settlement.settlement_ref}."
                        ),
                        weight=1.0,
                    )
                ],
            )
        )
        payment_key = normalize_reference(fee.payment_ref)
        if payment_key:
            payment_matches = payment_index.get(payment_key, [])
            if len(payment_matches) == 1:
                payment_to_settlement[payment_matches[0].id] = settlement

    payments_by_id = {payment.id: payment for payment in payments}
    for payment_id, settlement in payment_to_settlement.items():
        payment = payments_by_id[payment_id]
        links.append(
            ProposedLink(
                left=payment,
                right=settlement,
                relation=MatchRelation.PAYMENT_TO_SETTLEMENT,
                allocated_subunits=payment.amount_subunits,
                method=MatchMethod.EXACT_COMPOSITE,
                generator="exact:fee_bridge",
                score=1.0,
                risk=0.0,
                evidence=[
                    EvidenceDraft(
                        kind="fee_report_bridge",
                        statement=(
                            f"The fee report links payment {payment.payment_ref} and "
                            f"settlement {settlement.settlement_ref} in the same row, so the "
                            "payment settled in that batch."
                        ),
                        weight=0.95,
                    )
                ],
            )
        )

    # -- refund -> settlement, on the refund's own settlement reference -----
    #
    # A refund is deducted from the settlement window in which it was *issued*,
    # not from the window that paid out its original sale. Inferring the link
    # through the payment's settlement is therefore invalid: a refund raised a
    # week after the sale belongs to a later batch entirely. Only the refund's
    # own reference establishes it; rows that omit it are left for candidate
    # generation to propose against the settlement's refund total.
    for refund in refunds:
        key = normalize_reference(refund.settlement_ref)
        if not key:
            continue
        matches = settlement_index.get(key, [])
        if len(matches) != 1:
            if matches:
                ambiguous.add(key)
            continue
        settlement = matches[0]
        links.append(
            ProposedLink(
                left=refund,
                right=settlement,
                relation=MatchRelation.REFUND_TO_SETTLEMENT,
                allocated_subunits=-abs(refund.amount_subunits),
                method=MatchMethod.EXACT_REFERENCE,
                generator="exact:refund_settlement_ref",
                score=1.0,
                risk=0.0,
                evidence=[
                    EvidenceDraft(
                        kind="shared_settlement_reference",
                        statement=(
                            f"Refund {refund.external_id} names settlement "
                            f"{settlement.settlement_ref} directly."
                        ),
                        weight=1.0,
                    )
                ],
            )
        )

    # -- settlement -> bank credit, on UTR ---------------------------------
    bank_index = _index_by(bank_credits, "bank_ref_normalized")
    for settlement in settlements:
        key = normalize_reference(settlement.bank_ref)
        if not key:
            continue
        matches = bank_index.get(key, [])
        if len(matches) != 1:
            if matches:
                # Two credits carrying the same UTR: one is a duplicate. This
                # must reach a human, so no exact link is emitted.
                ambiguous.add(key)
            continue
        credit = matches[0]
        links.append(
            ProposedLink(
                left=settlement,
                right=credit,
                relation=MatchRelation.SETTLEMENT_TO_BANK_CREDIT,
                allocated_subunits=settlement.amount_subunits,
                method=MatchMethod.EXACT_REFERENCE,
                generator="exact:utr",
                score=1.0,
                risk=0.0,
                evidence=[
                    EvidenceDraft(
                        kind="shared_utr",
                        statement=(
                            f"Settlement UTR {settlement.bank_ref} appears verbatim in the "
                            f"bank narration for credit {credit.external_id}."
                        ),
                        weight=1.0,
                    )
                ],
            )
        )

    return links, ambiguous


def unresolved_records(
    records: list[SourceRecord], links: list[ProposedLink]
) -> dict[MatchRelation, list[SourceRecord]]:
    """Group records that still need a link, keyed by the relation they lack.

    Only these records enter candidate generation, which keeps the expensive
    stages proportional to the genuinely hard portion of the batch rather than
    to its total size.
    """
    matched_left: dict[MatchRelation, set[str]] = defaultdict(set)
    matched_right: dict[MatchRelation, set[str]] = defaultdict(set)
    for link in links:
        matched_left[link.relation].add(link.left.id)
        matched_right[link.relation].add(link.right.id)

    by_kind = _by_kind(records)
    pending: dict[MatchRelation, list[SourceRecord]] = {}

    pending[MatchRelation.ORDER_TO_PAYMENT] = [
        payment
        for payment in by_kind.get(RecordKind.PAYMENT, [])
        if payment.id not in matched_right[MatchRelation.ORDER_TO_PAYMENT]
    ]
    pending[MatchRelation.PAYMENT_TO_SETTLEMENT] = [
        payment
        for payment in by_kind.get(RecordKind.PAYMENT, [])
        if payment.id not in matched_left[MatchRelation.PAYMENT_TO_SETTLEMENT]
    ]
    pending[MatchRelation.PAYMENT_TO_REFUND] = [
        refund
        for refund in by_kind.get(RecordKind.REFUND, [])
        if refund.id not in matched_right[MatchRelation.PAYMENT_TO_REFUND]
    ]
    pending[MatchRelation.SETTLEMENT_TO_BANK_CREDIT] = [
        credit
        for credit in by_kind.get(RecordKind.BANK_CREDIT, [])
        if credit.id not in matched_right[MatchRelation.SETTLEMENT_TO_BANK_CREDIT]
    ]
    return pending
