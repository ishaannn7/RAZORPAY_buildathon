"""Feature extraction for candidate links.

Features are deliberately interpretable. Every one can be stated in a sentence a
finance reviewer would accept as a reason, which is what makes the counterfactual
explanations later in the pipeline meaningful rather than decorative.

Two features deserve note because they are about the *situation* rather than the
pair:

``competing_candidates``
    How many other records compete for this link. A pair that looks perfect in
    isolation is much weaker when three others look equally perfect, and a model
    that cannot see this cannot learn to abstain.

``reference_containment``
    Whether a truncated reference appears *inside* the other side's narration.
    Bank statements embed a shortened UTR in free text, so containment — not
    equality — is usually the only surviving link.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from reconproof.domain.entities import MatchRelation, PaymentMethod, RecordKind
from reconproof.ingest.keys import normalize_reference, reference_tail

if TYPE_CHECKING:
    from reconproof.db.models import SourceRecord

#: Expected settlement lag in days, by relation. Used to turn a raw date delta
#: into "how far from expected", which generalizes better than the raw value.
EXPECTED_LAG_DAYS: Final[dict[MatchRelation, float]] = {
    MatchRelation.ORDER_TO_PAYMENT: 0.05,
    MatchRelation.PAYMENT_TO_REFUND: 5.0,
    MatchRelation.PAYMENT_TO_SETTLEMENT: 2.0,
    MatchRelation.REFUND_TO_SETTLEMENT: 2.0,
    MatchRelation.SETTLEMENT_TO_BANK_CREDIT: 0.2,
    MatchRelation.FEE_TO_SETTLEMENT: 0.0,
}

FEATURE_NAMES: Final[tuple[str, ...]] = (
    "amount_exact",
    "amount_log_diff",
    "amount_rel_diff",
    "amount_unit_confusion",
    "currency_match",
    "day_delta",
    "day_delta_abs",
    "lag_deviation",
    "within_window",
    "reference_exact",
    "reference_containment",
    "reference_tail_match",
    "reference_similarity",
    "reference_jaro",
    "description_similarity",
    "counterparty_similarity",
    "method_compatible",
    "fee_consistent",
    "competing_candidates",
    "is_sole_candidate",
    "amount_rank",
    "semantic_similarity",
)


def _safe_log1p(value: float) -> float:
    return math.log1p(max(value, 0.0))


def _amount_features(left: SourceRecord, right: SourceRecord) -> dict[str, float]:
    left_amount = abs(left.amount_subunits)
    right_amount = abs(right.amount_subunits)
    difference = abs(left_amount - right_amount)
    larger = max(left_amount, right_amount, 1)
    smaller = min(left_amount, right_amount)
    return {
        "amount_exact": 1.0 if left_amount == right_amount else 0.0,
        "amount_log_diff": _safe_log1p(difference),
        "amount_rel_diff": difference / larger,
        # An exact 100x ratio is a unit error, not a coincidence, so it gets its
        # own feature rather than being buried in the relative difference.
        "amount_unit_confusion": 1.0 if smaller and larger == smaller * 100 else 0.0,
        "currency_match": 1.0 if left.currency == right.currency else 0.0,
    }


def _time_features(
    left: SourceRecord, right: SourceRecord, relation: MatchRelation, window_days: float
) -> dict[str, float]:
    if left.occurred_at is None or right.occurred_at is None:
        return {
            "day_delta": 0.0,
            "day_delta_abs": float(window_days),
            "lag_deviation": float(window_days),
            "within_window": 0.0,
        }
    delta_days = (right.occurred_at - left.occurred_at).total_seconds() / 86400
    expected = EXPECTED_LAG_DAYS.get(relation, 0.0)
    return {
        "day_delta": delta_days,
        "day_delta_abs": abs(delta_days),
        "lag_deviation": abs(delta_days - expected),
        "within_window": 1.0 if abs(delta_days) <= window_days else 0.0,
    }


def _reference_candidates(record: SourceRecord) -> list[str]:
    """Every reference-shaped string this record could be joined on."""
    values = [
        record.bank_ref_normalized,
        normalize_reference(record.bank_ref),
        normalize_reference(record.settlement_ref),
        normalize_reference(record.payment_ref),
        normalize_reference(record.order_ref),
        normalize_reference(record.external_id),
    ]
    return [value for value in values if value]


def _reference_features(left: SourceRecord, right: SourceRecord) -> dict[str, float]:
    left_refs = _reference_candidates(left)
    right_refs = _reference_candidates(right)
    left_text = (right.description_normalized or "") + " " + " ".join(right_refs)
    right_text = (left.description_normalized or "") + " " + " ".join(left_refs)

    exact = 0.0
    containment = 0.0
    similarity = 0.0
    jaro = 0.0

    for left_ref in left_refs:
        for right_ref in right_refs:
            if left_ref == right_ref:
                exact = 1.0
            # A truncated reference is a prefix of the full one. Containment in
            # either direction counts, since either side may be the truncated
            # copy.
            if len(left_ref) >= 5 and (left_ref in right_ref or right_ref in left_ref):
                containment = 1.0
            similarity = max(similarity, fuzz.ratio(left_ref, right_ref) / 100.0)
            jaro = max(jaro, JaroWinkler.similarity(left_ref, right_ref))

    # The reference may only exist inside free-text narration.
    for left_ref in left_refs:
        if len(left_ref) >= 6 and left_ref in _compact(left_text):
            containment = 1.0
    for right_ref in right_refs:
        if len(right_ref) >= 6 and right_ref in _compact(right_text):
            containment = 1.0

    left_tail = reference_tail(left.bank_ref or left.settlement_ref)
    right_tail = reference_tail(right.bank_ref or right.settlement_ref)
    tail_match = 1.0 if left_tail and right_tail and left_tail == right_tail else 0.0

    return {
        "reference_exact": exact,
        "reference_containment": containment,
        "reference_tail_match": tail_match,
        "reference_similarity": similarity,
        "reference_jaro": jaro,
    }


def _compact(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _text_features(left: SourceRecord, right: SourceRecord) -> dict[str, float]:
    left_description = left.description_normalized or ""
    right_description = right.description_normalized or ""
    description_similarity = (
        fuzz.token_set_ratio(left_description, right_description) / 100.0
        if left_description and right_description
        else 0.0
    )
    left_party = (left.counterparty or "").lower()
    right_party = (right.counterparty or "").lower()
    counterparty_similarity = (
        fuzz.token_set_ratio(left_party, right_party) / 100.0 if left_party and right_party else 0.0
    )
    return {
        "description_similarity": description_similarity,
        "counterparty_similarity": counterparty_similarity,
    }


def _consistency_features(
    left: SourceRecord, right: SourceRecord, relation: MatchRelation
) -> dict[str, float]:
    method_compatible = 1.0
    if (
        left.payment_method is not PaymentMethod.UNKNOWN
        and right.payment_method is not PaymentMethod.UNKNOWN
    ):
        method_compatible = 1.0 if left.payment_method == right.payment_method else 0.0

    # For a payment settling into a batch, the recomputed fee should be
    # consistent with the fee the payment reports. A large divergence is
    # evidence against the link.
    fee_consistent = 1.0
    if relation is MatchRelation.PAYMENT_TO_SETTLEMENT and left.record_kind is RecordKind.PAYMENT:
        reported = (left.fee_subunits or 0) + (left.tax_subunits or 0)
        if reported and left.amount_subunits:
            implied_rate = reported / abs(left.amount_subunits)
            # 2% commission plus 18% GST on it is ~2.36% all-in.
            fee_consistent = 1.0 if 0.005 <= implied_rate <= 0.06 else 0.0
    return {"method_compatible": method_compatible, "fee_consistent": fee_consistent}


def extract_features(
    left: SourceRecord,
    right: SourceRecord,
    relation: MatchRelation,
    *,
    window_days: float,
    competing_candidates: int = 1,
    amount_rank: int = 0,
    semantic_similarity: float = 0.0,
) -> dict[str, float]:
    """Compute the full feature vector for one candidate pair."""
    features: dict[str, float] = {}
    features.update(_amount_features(left, right))
    features.update(_time_features(left, right, relation, window_days))
    features.update(_reference_features(left, right))
    features.update(_text_features(left, right))
    features.update(_consistency_features(left, right, relation))
    features["competing_candidates"] = float(competing_candidates)
    features["is_sole_candidate"] = 1.0 if competing_candidates <= 1 else 0.0
    features["amount_rank"] = float(amount_rank)
    features["semantic_similarity"] = float(semantic_similarity)
    return {name: float(features.get(name, 0.0)) for name in FEATURE_NAMES}


def to_vector(features: dict[str, float]) -> list[float]:
    """Order a feature dict into the fixed vector the model expects."""
    return [float(features.get(name, 0.0)) for name in FEATURE_NAMES]


def describe_feature(name: str, value: float) -> str:
    """Render one feature as a reviewer-readable statement."""
    templates = {
        "amount_exact": lambda v: "The amounts are identical." if v else "The amounts differ.",
        "reference_exact": lambda v: (
            "The references match exactly." if v else "No reference matches exactly."
        ),
        "reference_containment": lambda v: (
            "One reference appears inside the other side's text."
            if v
            else "Neither reference appears in the other side's text."
        ),
        "reference_tail_match": lambda v: (
            "The trailing digits of both references agree."
            if v
            else "The trailing reference digits do not agree."
        ),
        "within_window": lambda v: (
            "The dates fall inside the expected settlement window."
            if v
            else "The dates fall outside the expected settlement window."
        ),
        "is_sole_candidate": lambda v: (
            "No other record competes for this link."
            if v
            else "Other records compete for this link."
        ),
        "amount_unit_confusion": lambda v: (
            "The two amounts differ by exactly 100x, which suggests a paise/rupee error."
            if v
            else "The amounts are not a 100x multiple of each other."
        ),
        "currency_match": lambda v: (
            "Both records are in the same currency."
            if v
            else "The records are in different currencies."
        ),
    }
    template = templates.get(name)
    if template is not None:
        return template(value)
    if name == "day_delta_abs":
        return f"The records are {value:.1f} days apart."
    if name == "lag_deviation":
        return f"The gap deviates from the expected lag by {value:.1f} days."
    if name == "reference_similarity":
        return f"Reference string similarity is {value:.0%}."
    if name == "competing_candidates":
        return f"{int(value)} record(s) compete for this link."
    return f"{name} = {value:.3f}"
