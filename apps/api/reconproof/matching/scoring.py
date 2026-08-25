"""Match scoring, calibration and selective risk control.

A probability alone is not a licence to post to a ledger. ``0.93`` from an
uncalibrated model means very little, and even a well-calibrated ``0.99`` says
nothing about how often *the accepted set as a whole* is wrong.

So acceptance is governed by a threshold chosen on held-out calibration data
using a finite-sample lower bound on precision. This is conformal risk control
in the Learn-then-Test sense: the threshold is a parameter selected so that a
bound on the risk of the selected set holds with high probability, rather than a
number picked because it looked reasonable on a chart.

The practical consequence is that a small calibration set produces a
conservative threshold. That is the correct behaviour — with little evidence the
system should automate less, not guess more.
"""

from __future__ import annotations

import json
import math
import pickle
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from reconproof.matching.features import FEATURE_NAMES, to_vector

#: Confidence level for the one-sided precision lower bound.
DEFAULT_CONFIDENCE = 0.95


def wilson_lower_bound(
    successes: int, trials: int, confidence: float = DEFAULT_CONFIDENCE
) -> float:
    """One-sided Wilson lower bound on a binomial proportion.

    Preferred over the raw ratio because ``5/5`` and ``500/500`` are both 100%
    empirically but carry very different evidence. Using the bound is what makes
    a small calibration set produce a conservative threshold instead of an
    overconfident one.
    """
    if trials <= 0:
        return 0.0
    # Normal quantile for a one-sided interval at the requested confidence.
    z = _normal_quantile(confidence)
    phat = successes / trials
    denominator = 1.0 + z * z / trials
    centre = phat + z * z / (2 * trials)
    margin = z * math.sqrt(phat * (1 - phat) / trials + z * z / (4 * trials * trials))
    return max(0.0, (centre - margin) / denominator)


def _normal_quantile(confidence: float) -> float:
    """Inverse standard normal CDF via a rational approximation."""
    p = min(max(confidence, 1e-6), 1 - 1e-6)
    # Acklam's algorithm, sufficient for threshold selection.
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )


@dataclass(slots=True)
class RiskCurvePoint:
    threshold: float
    selected: int
    correct: int
    empirical_precision: float
    precision_lower_bound: float

    @property
    def risk(self) -> float:
        return 1.0 - self.precision_lower_bound


@dataclass(slots=True)
class CalibrationOutcome:
    """The thresholds and evidence produced by one calibration run."""

    accept_threshold: float
    review_threshold: float
    risk_budget: float
    target_precision: float
    confidence: float
    achieved_precision_lower_bound: float
    achieved_empirical_precision: float
    coverage_at_accept: float
    calibration_size: int
    positives: int
    curve: list[RiskCurvePoint] = field(default_factory=list)
    #: True when no threshold satisfied the target. Automation is disabled
    #: entirely rather than run at an unproven threshold.
    automation_disabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["curve"] = [asdict(point) for point in self.curve]
        return payload


def _outcome_from_dict(payload: dict[str, Any]) -> CalibrationOutcome:
    data = dict(payload)
    curve = [RiskCurvePoint(**point) for point in data.pop("curve", [])]
    return CalibrationOutcome(**data, curve=curve)


class HeuristicScorer:
    """Untrained weighted baseline.

    Exists so the calibrated model's contribution can be quantified. A learned
    model that cannot beat a sensible hand-weighted rule is not worth the
    complexity it adds, and reporting only the model's own numbers would hide
    that comparison.
    """

    WEIGHTS: ClassVar[Mapping[str, float]] = {
        "reference_exact": 3.0,
        "reference_containment": 2.0,
        "reference_tail_match": 1.5,
        "reference_similarity": 1.0,
        "reference_jaro": 0.6,
        "amount_exact": 1.5,
        "within_window": 0.8,
        "is_sole_candidate": 1.0,
        "currency_match": 0.5,
        "description_similarity": 0.3,
        "amount_unit_confusion": -3.0,
        "lag_deviation": -0.08,
        "competing_candidates": -0.15,
    }
    BIAS = -2.2

    def score_one(self, features: dict[str, float]) -> float:
        total = self.BIAS
        for name, weight in self.WEIGHTS.items():
            total += weight * features.get(name, 0.0)
        return 1.0 / (1.0 + math.exp(-total))

    def score(self, feature_rows: list[dict[str, float]]) -> list[float]:
        return [self.score_one(row) for row in feature_rows]


class CalibratedMatchScorer:
    """Logistic model plus selective risk control.

    Logistic regression is chosen over a gradient-boosted alternative on
    purpose: coefficients map directly onto the interpretable features, which
    keeps counterfactual explanations faithful to what the model actually did.
    The evaluation harness reports both, so the choice is defended with numbers
    rather than asserted.
    """

    def __init__(self, feature_names: tuple[str, ...] = FEATURE_NAMES) -> None:
        self.feature_names = feature_names
        self.pipeline: Pipeline | None = None
        self.calibration: CalibrationOutcome | None = None
        #: Per-relation thresholds. Relations differ enormously in how much
        #: evidence they carry: a settlement's UTR appearing in a bank narration
        #: is near-conclusive, while attributing one refund inside a settlement's
        #: refund total is inherently ambiguous. A single global threshold has to
        #: satisfy the hardest relation, so it either disables automation
        #: entirely or over-automates the easy ones. Calibrating each relation
        #: against its own evidence avoids that trade.
        self.calibration_by_relation: dict[str, CalibrationOutcome] = {}
        self.training_rows = 0
        self.training_positives = 0

    # -- fitting -----------------------------------------------------------

    def fit(self, feature_rows: list[dict[str, float]], labels: list[int]) -> None:
        if not feature_rows:
            raise ValueError("cannot fit on an empty feature set")
        matrix = np.asarray([to_vector(row) for row in feature_rows], dtype=float)
        target = np.asarray(labels, dtype=int)
        if len(set(labels)) < 2:
            raise ValueError(
                "training data contains a single class; a scorer fitted on it would be meaningless"
            )
        self.pipeline = Pipeline(
            [
                ("scale", StandardScaler()),
                (
                    "model",
                    LogisticRegression(
                        max_iter=2000,
                        C=1.0,
                        # Candidate sets are dominated by negatives. Balancing
                        # keeps the model from trivially predicting "no match".
                        class_weight="balanced",
                        solver="lbfgs",
                    ),
                ),
            ]
        )
        self.pipeline.fit(matrix, target)
        self.training_rows = len(labels)
        self.training_positives = int(target.sum())

    # -- prediction --------------------------------------------------------

    def raw_scores(self, feature_rows: list[dict[str, float]]) -> list[float]:
        if self.pipeline is None:
            raise RuntimeError("scorer is not fitted")
        if not feature_rows:
            return []
        matrix = np.asarray([to_vector(row) for row in feature_rows], dtype=float)
        return [float(value) for value in self.pipeline.predict_proba(matrix)[:, 1]]

    def outcome_for(self, relation: str | None) -> CalibrationOutcome | None:
        """The calibration governing *relation*, falling back to the global one.

        A relation with no calibration evidence of its own must not silently
        borrow another relation's threshold, so the fallback is the global
        outcome, which was fitted across everything.
        """
        if relation is not None:
            specific = self.calibration_by_relation.get(relation)
            if specific is not None:
                return specific
        return self.calibration

    def risk_for_score(self, score: float, relation: str | None = None) -> float:
        """Estimated probability that accepting at *score* is wrong.

        Read off the calibration curve rather than from the model's own output,
        so the number reflects observed selective performance instead of the
        model's self-assessment.
        """
        outcome = self.outcome_for(relation)
        if outcome is None or not outcome.curve:
            return 1.0
        if outcome.automation_disabled:
            # No threshold proved the target for this relation. Reporting a low
            # risk here would let the policy engine automate on evidence that
            # was never established.
            return 1.0
        best = 1.0
        for point in outcome.curve:
            if score >= point.threshold:
                best = min(best, point.risk)
        return best

    # -- calibration -------------------------------------------------------

    def calibrate(
        self,
        feature_rows: list[dict[str, float]],
        labels: list[int],
        *,
        target_precision: float = 0.99,
        risk_budget: float = 0.01,
        confidence: float = DEFAULT_CONFIDENCE,
        review_floor: float = 0.35,
        grid_size: int = 200,
    ) -> CalibrationOutcome:
        """Choose the acceptance threshold on held-out calibration data.

        The chosen threshold is the lowest one whose precision *lower bound*
        still clears the target, which maximizes coverage subject to the risk
        guarantee rather than trading the guarantee away for coverage.
        """
        scores = self.raw_scores(feature_rows)
        target = np.asarray(labels, dtype=int)
        positives = int(target.sum())

        curve: list[RiskCurvePoint] = []
        # Coerced to plain floats: NumPy scalars leak into the persisted
        # metadata and are not JSON-serializable.
        candidates = sorted({round(float(value), 4) for value in np.linspace(0.0, 1.0, grid_size)})
        for threshold in candidates:
            selected_mask = [score >= threshold for score in scores]
            selected = int(sum(selected_mask))
            if selected == 0:
                continue
            correct = int(
                sum(1 for flag, y in zip(selected_mask, labels, strict=True) if flag and y)
            )
            empirical = correct / selected
            bound = wilson_lower_bound(correct, selected, confidence)
            curve.append(
                RiskCurvePoint(
                    threshold=threshold,
                    selected=selected,
                    correct=correct,
                    empirical_precision=empirical,
                    precision_lower_bound=bound,
                )
            )

        viable = [
            point
            for point in curve
            if point.precision_lower_bound >= target_precision
            and (1.0 - point.precision_lower_bound) <= risk_budget
        ]
        if viable:
            # Lowest qualifying threshold: maximum coverage at the guaranteed risk.
            chosen = min(viable, key=lambda point: point.threshold)
            outcome = CalibrationOutcome(
                accept_threshold=chosen.threshold,
                review_threshold=min(review_floor, chosen.threshold),
                risk_budget=risk_budget,
                target_precision=target_precision,
                confidence=confidence,
                achieved_precision_lower_bound=chosen.precision_lower_bound,
                achieved_empirical_precision=chosen.empirical_precision,
                coverage_at_accept=chosen.selected / max(len(scores), 1),
                calibration_size=len(scores),
                positives=positives,
                curve=curve,
            )
        else:
            # No threshold proves the target. Automation is switched off rather
            # than run at an unproven level; everything routes to review.
            outcome = CalibrationOutcome(
                accept_threshold=1.01,
                review_threshold=review_floor,
                risk_budget=risk_budget,
                target_precision=target_precision,
                confidence=confidence,
                achieved_precision_lower_bound=max(
                    (point.precision_lower_bound for point in curve), default=0.0
                ),
                achieved_empirical_precision=max(
                    (point.empirical_precision for point in curve), default=0.0
                ),
                coverage_at_accept=0.0,
                calibration_size=len(scores),
                positives=positives,
                curve=curve,
                automation_disabled=True,
            )
        self.calibration = outcome
        return outcome

    def calibrate_by_relation(
        self,
        feature_rows: list[dict[str, float]],
        labels: list[int],
        relations: list[str],
        *,
        target_precision: float = 0.99,
        risk_budget: float = 0.01,
        confidence: float = DEFAULT_CONFIDENCE,
        review_floor: float = 0.35,
        min_calibration_rows: int = 120,
    ) -> dict[str, CalibrationOutcome]:
        """Calibrate a separate threshold per relation, plus a global fallback.

        A relation with fewer than *min_calibration_rows* calibration examples
        is left without its own threshold and falls back to the global one.
        Fitting a threshold on a handful of rows would produce a Wilson bound so
        wide it either blocks everything or, worse, looks acceptable by
        coincidence.
        """
        self.calibrate(
            feature_rows,
            labels,
            target_precision=target_precision,
            risk_budget=risk_budget,
            confidence=confidence,
            review_floor=review_floor,
        )

        buckets: dict[str, list[int]] = {}
        for index, relation in enumerate(relations):
            buckets.setdefault(relation, []).append(index)

        self.calibration_by_relation = {}
        for relation, indices in buckets.items():
            if len(indices) < min_calibration_rows:
                continue
            subset_rows = [feature_rows[index] for index in indices]
            subset_labels = [labels[index] for index in indices]
            if len(set(subset_labels)) < 2:
                # Without both classes there is nothing to separate, so a
                # threshold here would be meaningless.
                continue
            scorer_view = CalibratedMatchScorer(self.feature_names)
            scorer_view.pipeline = self.pipeline
            outcome = scorer_view.calibrate(
                subset_rows,
                subset_labels,
                target_precision=target_precision,
                risk_budget=risk_budget,
                confidence=confidence,
                review_floor=review_floor,
            )
            self.calibration_by_relation[relation] = outcome
        return self.calibration_by_relation

    # -- inspection --------------------------------------------------------

    def coefficients(self) -> dict[str, float]:
        """Standardized coefficients, for the model card and explanations."""
        if self.pipeline is None:
            return {}
        model = self.pipeline.named_steps["model"]
        return {
            name: float(weight)
            for name, weight in zip(self.feature_names, model.coef_[0], strict=True)
        }

    def reliability(
        self, feature_rows: list[dict[str, float]], labels: list[int], bins: int = 10
    ) -> dict[str, Any]:
        """Reliability diagram data and expected calibration error."""
        scores = self.raw_scores(feature_rows)
        if not scores:
            return {"bins": [], "expected_calibration_error": None}
        edges = np.linspace(0.0, 1.0, bins + 1)
        rows: list[dict[str, float]] = []
        ece = 0.0
        total = len(scores)
        for index in range(bins):
            low, high = edges[index], edges[index + 1]
            members = [
                (score, label)
                for score, label in zip(scores, labels, strict=True)
                if (score >= low and score < high) or (index == bins - 1 and score == 1.0)
            ]
            if not members:
                continue
            mean_score = sum(score for score, _ in members) / len(members)
            observed = sum(label for _, label in members) / len(members)
            rows.append(
                {
                    "bin_low": float(low),
                    "bin_high": float(high),
                    "count": len(members),
                    "mean_predicted": mean_score,
                    "observed_frequency": observed,
                }
            )
            ece += (len(members) / total) * abs(mean_score - observed)
        return {"bins": rows, "expected_calibration_error": ece}

    # -- persistence -------------------------------------------------------

    def save(self, directory: Path) -> Path:
        if self.pipeline is None:
            raise RuntimeError("cannot save an unfitted scorer")
        directory.mkdir(parents=True, exist_ok=True)
        model_path = directory / "model.pkl"
        with model_path.open("wb") as handle:
            pickle.dump(self.pipeline, handle)
        metadata = {
            "feature_names": list(self.feature_names),
            "training_rows": self.training_rows,
            "training_positives": self.training_positives,
            "calibration": self.calibration.to_dict() if self.calibration else None,
            "calibration_by_relation": {
                relation: outcome.to_dict()
                for relation, outcome in self.calibration_by_relation.items()
            },
            "coefficients": self.coefficients(),
        }
        (directory / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return model_path

    @classmethod
    def load(cls, directory: Path) -> CalibratedMatchScorer:
        scorer = cls()
        with (directory / "model.pkl").open("rb") as handle:
            scorer.pipeline = pickle.load(handle)
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        scorer.feature_names = tuple(metadata["feature_names"])
        scorer.training_rows = metadata.get("training_rows", 0)
        scorer.training_positives = metadata.get("training_positives", 0)
        calibration = metadata.get("calibration")
        if calibration:
            scorer.calibration = _outcome_from_dict(calibration)
        scorer.calibration_by_relation = {
            relation: _outcome_from_dict(payload)
            for relation, payload in (metadata.get("calibration_by_relation") or {}).items()
        }
        return scorer
