"""Per-candidate feature contributions.

The claim under test: `contributions_for` is not a plausible-looking
approximation, it is the *exact* decomposition the logistic model already
uses internally — `sum(contribution) + intercept` must reconstruct the
model's own logit for that row bit-for-bit (asserted via the model's own
`decision_function`, not re-derived by hand), and the API must actually
attach these to a real candidate rather than merely defining the schema.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from reconproof.config import Settings
from reconproof.learning.training import materialize_dataset
from reconproof.main import create_app
from reconproof.matching.features import FEATURE_NAMES
from reconproof.matching.scoring import CalibratedMatchScorer
from reconproof.pipeline import ReconciliationPipeline
from reconproof.runtime import scorer_dir
from reconproof.synthetic.generator import DatasetSpec


def _synthetic_training_set(
    n: int = 200, seed: int = 11
) -> tuple[list[dict[str, float]], list[int]]:
    rng = np.random.default_rng(seed)
    rows: list[dict[str, float]] = []
    labels: list[int] = []
    for _ in range(n):
        values = rng.uniform(0.0, 1.0, size=len(FEATURE_NAMES))
        row = dict(zip(FEATURE_NAMES, values, strict=True))
        # A simple, known-ish rule so the fit isn't degenerate: high amount
        # match and reference match should predict positive more often.
        label = int(row["amount_exact"] > 0.5 and row["reference_exact"] > 0.4)
        rows.append(row)
        labels.append(label)
    # Guarantee both classes are present regardless of the random draw.
    rows.append(dict.fromkeys(FEATURE_NAMES, 1.0))
    labels.append(1)
    rows.append(dict.fromkeys(FEATURE_NAMES, 0.0))
    labels.append(0)
    return rows, labels


@pytest.fixture()
def fitted_scorer() -> CalibratedMatchScorer:
    rows, labels = _synthetic_training_set()
    scorer = CalibratedMatchScorer()
    scorer.fit(rows, labels)
    return scorer


class TestContributionsReconstructTheLogit:
    def test_sum_of_contributions_plus_intercept_equals_the_model_logit(
        self, fitted_scorer: CalibratedMatchScorer
    ) -> None:
        row = {name: 0.3 + 0.02 * index for index, name in enumerate(FEATURE_NAMES)}
        contributions = fitted_scorer.contributions_for(row)
        assert len(contributions) == len(FEATURE_NAMES)

        model = fitted_scorer.pipeline.named_steps["model"]  # type: ignore[union-attr]
        scale = fitted_scorer.pipeline.named_steps["scale"]  # type: ignore[union-attr]
        matrix = scale.transform(np.asarray([[row[name] for name in FEATURE_NAMES]]))
        expected_logit = float(model.decision_function(matrix)[0])

        reconstructed = sum(entry["contribution"] for entry in contributions) + float(
            model.intercept_[0]
        )
        assert reconstructed == pytest.approx(expected_logit, abs=1e-9)

    def test_contributions_are_sorted_by_absolute_magnitude_descending(
        self, fitted_scorer: CalibratedMatchScorer
    ) -> None:
        row = {name: 0.3 + 0.02 * index for index, name in enumerate(FEATURE_NAMES)}
        contributions = fitted_scorer.contributions_for(row)
        magnitudes = [abs(entry["contribution"]) for entry in contributions]
        assert magnitudes == sorted(magnitudes, reverse=True)

    def test_raw_values_are_echoed_back_unchanged(
        self, fitted_scorer: CalibratedMatchScorer
    ) -> None:
        row = {name: 0.3 + 0.02 * index for index, name in enumerate(FEATURE_NAMES)}
        by_feature = {entry["feature"]: entry for entry in fitted_scorer.contributions_for(row)}
        for name, value in row.items():
            assert by_feature[name]["raw_value"] == pytest.approx(value)

    def test_missing_features_default_to_zero_like_to_vector_does(
        self, fitted_scorer: CalibratedMatchScorer
    ) -> None:
        contributions = fitted_scorer.contributions_for({"amount_exact": 1.0})
        by_feature = {entry["feature"]: entry for entry in contributions}
        assert by_feature["reference_exact"]["raw_value"] == 0.0

    def test_unfitted_scorer_returns_no_contributions(self) -> None:
        assert CalibratedMatchScorer().contributions_for({"amount_exact": 1.0}) == []


class TestFeatureContributionsOnTheExceptionRoute:
    def test_a_real_candidate_gets_real_contributions_from_the_active_scorer(
        self, db: Session, settings: Settings, tmp_path: Path, fitted_scorer: CalibratedMatchScorer
    ) -> None:
        # A real reconciled batch, so the candidate's `features` come from the
        # actual feature-extraction path, not a hand-built dict.
        batch, _truth = materialize_dataset(
            db, DatasetSpec(name="contributions", seed=606, n_orders=200), tmp_path / "contrib"
        )
        ReconciliationPipeline(db, settings=settings).run(batch)
        db.commit()

        # Save the fitted scorer where `load_active_scorer` looks for it.
        directory = scorer_dir(settings)
        fitted_scorer.save(directory)

        client = TestClient(create_app())
        exceptions = client.get(f"/api/exceptions?batch_id={batch.id}&limit=50").json()
        target = next(
            (
                item
                for item in exceptions
                if client.get(f"/api/exceptions/{item['id']}").json()["candidates"]
            ),
            None,
        )
        assert target is not None, "fixture batch produced no exception with any candidate"

        detail = client.get(f"/api/exceptions/{target['id']}").json()
        candidate = detail["candidates"][0]
        assert candidate["feature_contributions"], "expected non-empty contributions"
        contribution_features = {row["feature"] for row in candidate["feature_contributions"]}
        assert contribution_features == set(FEATURE_NAMES)
        # Same reconstruction property, now proven end to end through the API.
        model = fitted_scorer.pipeline.named_steps["model"]  # type: ignore[union-attr]
        scale = fitted_scorer.pipeline.named_steps["scale"]  # type: ignore[union-attr]
        matrix = scale.transform(
            np.asarray([[candidate["features"].get(name, 0.0) for name in FEATURE_NAMES]])
        )
        expected_logit = float(model.decision_function(matrix)[0])
        reconstructed = sum(
            row["contribution"] for row in candidate["feature_contributions"]
        ) + float(model.intercept_[0])
        assert reconstructed == pytest.approx(expected_logit, abs=1e-6)

    def test_no_trained_scorer_means_empty_contributions_not_an_error(
        self, db: Session, settings: Settings, tmp_path: Path
    ) -> None:
        batch, _truth = materialize_dataset(
            db, DatasetSpec(name="no-scorer", seed=707, n_orders=150), tmp_path / "no-scorer"
        )
        ReconciliationPipeline(db, settings=settings).run(batch)
        db.commit()

        client = TestClient(create_app())
        exceptions = client.get(f"/api/exceptions?batch_id={batch.id}&limit=50").json()
        assert exceptions
        detail = client.get(f"/api/exceptions/{exceptions[0]['id']}").json()
        assert detail["candidates"] == [] or all(
            candidate["feature_contributions"] == [] for candidate in detail["candidates"]
        )
