"""Training and evaluation harness.

The model is trained on one generated dataset and evaluated on a *different*
one, generated from a different seed. That is stronger than a row-level split of
a single dataset: nothing about the evaluation period was available at fit time,
not even indirectly through a shared settlement batch.

Every number this module reports is accompanied by the baseline it should be
compared against. A calibrated model that cannot beat plain reference matching
and a hand-weighted heuristic has not earned its place in the pipeline, and
reporting it alone would hide that.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from reconproof.config import Settings, get_settings
from reconproof.db.models import ReconciliationBatch, SourceRecord
from reconproof.domain.entities import BatchStatus, SourceKind
from reconproof.ingest.loader import ingest_file
from reconproof.learning.labeling import LabeledCandidates, TruthSet, label_candidates
from reconproof.matching.candidates import generate_all_candidates
from reconproof.matching.exact import match_exact
from reconproof.matching.scoring import CalibratedMatchScorer, HeuristicScorer, wilson_lower_bound
from reconproof.matching.types import ProposedLink
from reconproof.pipeline import ReconciliationPipeline
from reconproof.synthetic.generator import DatasetSpec, write_dataset


@dataclass(slots=True)
class ClassificationMetrics:
    threshold: float
    selected: int
    true_positives: int
    false_positives: int
    false_negatives: int
    total_positives: int

    @property
    def precision(self) -> float:
        return self.true_positives / self.selected if self.selected else 0.0

    @property
    def recall(self) -> float:
        return self.true_positives / self.total_positives if self.total_positives else 0.0

    @property
    def f1(self) -> float:
        precision, recall = self.precision, self.recall
        return 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    @property
    def precision_lower_bound(self) -> float:
        return wilson_lower_bound(self.true_positives, self.selected)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            {
                "precision": self.precision,
                "recall": self.recall,
                "f1": self.f1,
                "precision_lower_bound": self.precision_lower_bound,
            }
        )
        return payload


@dataclass(slots=True)
class EvaluationReport:
    dataset: str
    seed: int
    candidates: int
    positives: int
    model: dict[str, Any] = field(default_factory=dict)
    baselines: dict[str, Any] = field(default_factory=dict)
    per_corruption: dict[str, Any] = field(default_factory=dict)
    calibration: dict[str, Any] = field(default_factory=dict)
    coefficients: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_at_threshold(
    scores: list[float], labels: list[int], threshold: float
) -> ClassificationMetrics:
    selected = 0
    true_positives = 0
    for score, label in zip(scores, labels, strict=True):
        if score >= threshold:
            selected += 1
            true_positives += label
    total_positives = sum(labels)
    return ClassificationMetrics(
        threshold=threshold,
        selected=selected,
        true_positives=true_positives,
        false_positives=selected - true_positives,
        false_negatives=total_positives - true_positives,
        total_positives=total_positives,
    )


def build_candidates(records: list[SourceRecord], settings: Settings) -> list[ProposedLink]:
    """Produce the candidate set for a batch, without scoring it.

    Delegates to the same generator the pipeline uses, so training and
    evaluation cannot drift away from what runs in production.
    """
    exact_links, _ = match_exact(records)
    return generate_all_candidates(
        records,
        exact_links,
        window_days=settings.candidate_day_window,
        amount_tolerance_bps=settings.candidate_amount_tolerance_bps,
        max_candidates_per_record=settings.max_candidates_per_record,
    )


def materialize_dataset(
    session: Session,
    spec: DatasetSpec,
    directory: Path,
    *,
    name: str | None = None,
) -> tuple[ReconciliationBatch, TruthSet]:
    """Generate, write and ingest a dataset into a fresh batch."""
    write_dataset(spec, directory)
    truth = TruthSet.from_manifest(directory / "truth.json")
    lookup = {"groups": truth.groups, "corruptions": truth.corruptions}

    batch = ReconciliationBatch(
        name=name or spec.name,
        status=BatchStatus.READY,
        currency=spec.currency,
        dataset_seed=spec.seed,
    )
    session.add(batch)
    session.flush()

    for source_kind in SourceKind:
        path = directory / f"{source_kind.value}.csv"
        if not path.exists():
            continue
        ingest_file(
            session,
            batch=batch,
            source_kind=source_kind,
            filename=path.name,
            payload=path.read_bytes(),
            truth_lookup=lookup,
        )
    session.flush()
    return batch, truth


def collect_labeled(
    session: Session,
    spec: DatasetSpec,
    directory: Path,
    settings: Settings,
    *,
    suffix: str,
) -> tuple[LabeledCandidates, ReconciliationBatch, TruthSet]:
    """Materialize a dataset and label the candidates the model will score.

    Deterministically settled links are excluded: the model never scores them,
    so including them would pad the set with decisions the threshold does not
    govern and make both fitting and calibration describe the wrong population.
    """
    batch, truth = materialize_dataset(session, spec, directory, name=f"{spec.name}-{suffix}")
    records = list(session.query(SourceRecord).filter(SourceRecord.batch_id == batch.id).all())
    candidates = [link for link in build_candidates(records, settings) if not link.scoring_final]
    return label_candidates(candidates, truth), batch, truth


def train_scorer(
    session: Session,
    *,
    spec: DatasetSpec,
    calibration_spec: DatasetSpec,
    directory: Path,
    settings: Settings | None = None,
) -> tuple[CalibratedMatchScorer, LabeledCandidates, LabeledCandidates]:
    """Fit on one dataset and calibrate on a separate, realistically distributed one.

    The split matters more than it looks. The fitting dataset is deliberately
    augmented with extra truncated references, typos and same-amount decoys,
    because the model needs to see hard cases often enough to learn from them.

    Calibration must *not* use that distribution. A risk bound measured on
    adversarially hard data does not transfer to production: it would be
    pessimistic in a way that silently disables automation, and if the
    augmentation were ever milder than reality it would be optimistic in a way
    that silently permits it. So the threshold is calibrated on a dataset drawn
    with realistic corruption rates, which is the distribution the guarantee is
    supposed to describe.
    """
    settings = settings or get_settings()
    train, _, _ = collect_labeled(session, spec, directory / "fit", settings, suffix="fit")
    calibration, _, _ = collect_labeled(
        session, calibration_spec, directory / "calibrate", settings, suffix="calibrate"
    )

    scorer = CalibratedMatchScorer()
    scorer.fit(train.feature_rows, train.labels)
    scorer.calibrate_by_relation(
        calibration.feature_rows,
        calibration.labels,
        [link.relation.value for link in calibration.links],
        target_precision=settings.target_precision,
        risk_budget=settings.risk_budget,
        review_floor=settings.review_score_floor,
    )
    return scorer, train, calibration


def evaluate_scorer(
    session: Session,
    scorer: CalibratedMatchScorer,
    *,
    spec: DatasetSpec,
    directory: Path,
    settings: Settings | None = None,
) -> tuple[EvaluationReport, ReconciliationBatch, TruthSet]:
    """Evaluate on a held-out dataset generated from a different seed."""
    settings = settings or get_settings()
    batch, truth = materialize_dataset(session, spec, directory, name=f"{spec.name}-test")
    records = list(session.query(SourceRecord).filter(SourceRecord.batch_id == batch.id).all())
    candidates = [link for link in build_candidates(records, settings) if not link.scoring_final]
    labeled = label_candidates(candidates, truth)

    report = EvaluationReport(
        dataset=spec.name,
        seed=spec.seed,
        candidates=len(labeled),
        positives=labeled.positives,
    )

    if not labeled.links:
        return report, batch, truth

    calibration = scorer.calibration
    model_scores = scorer.raw_scores(labeled.feature_rows)
    relations = [link.relation.value for link in labeled.links]

    # Each candidate is judged against the threshold governing *its* relation,
    # which is what the pipeline does. Scoring everything against one global
    # threshold would report a system that does not exist.
    accepted_flags: list[bool] = []
    for score, relation in zip(model_scores, relations, strict=True):
        outcome = scorer.outcome_for(relation)
        threshold = outcome.accept_threshold if outcome else 1.01
        disabled = outcome.automation_disabled if outcome else True
        accepted_flags.append((not disabled) and score >= threshold)

    selected = sum(accepted_flags)
    true_positives = sum(
        1 for flag, label in zip(accepted_flags, labeled.labels, strict=True) if flag and label
    )
    total_positives = labeled.positives
    report.model = {
        "selection": "per_relation_thresholds",
        "selected": selected,
        "true_positives": true_positives,
        "false_positives": selected - true_positives,
        "false_negatives": total_positives - true_positives,
        "total_positives": total_positives,
        "precision": (true_positives / selected) if selected else 0.0,
        "recall": (true_positives / total_positives) if total_positives else 0.0,
        "precision_lower_bound": wilson_lower_bound(true_positives, selected),
        "risk_budget": calibration.risk_budget if calibration else None,
    }
    precision = report.model["precision"]
    recall = report.model["recall"]
    report.model["f1"] = (
        2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    )

    per_relation: dict[str, Any] = {}
    for relation in sorted(set(relations)):
        indices = [index for index, name in enumerate(relations) if name == relation]
        outcome = scorer.outcome_for(relation)
        relation_selected = sum(accepted_flags[index] for index in indices)
        relation_true = sum(
            1 for index in indices if accepted_flags[index] and labeled.labels[index]
        )
        relation_positives = sum(labeled.labels[index] for index in indices)
        per_relation[relation] = {
            "candidates": len(indices),
            "positives": relation_positives,
            "threshold": outcome.accept_threshold if outcome else None,
            "automation_disabled": outcome.automation_disabled if outcome else True,
            "has_own_calibration": relation in scorer.calibration_by_relation,
            "selected": relation_selected,
            "true_positives": relation_true,
            "false_positives": relation_selected - relation_true,
            "precision": (relation_true / relation_selected) if relation_selected else None,
            "recall": (relation_true / relation_positives) if relation_positives else None,
            "precision_lower_bound": wilson_lower_bound(relation_true, relation_selected),
        }
    report.model["per_relation"] = per_relation

    heuristic = HeuristicScorer()
    heuristic_scores = heuristic.score(labeled.feature_rows)
    # The heuristic is evaluated at the threshold that maximizes its own F1, so
    # the comparison is against the baseline at its best rather than at a
    # threshold chosen to make the model look good.
    best_heuristic: ClassificationMetrics | None = None
    for step in range(5, 100, 5):
        threshold = step / 100
        metrics = evaluate_at_threshold(heuristic_scores, labeled.labels, threshold)
        if best_heuristic is None or metrics.f1 > best_heuristic.f1:
            best_heuristic = metrics
    report.baselines["heuristic_best_f1"] = best_heuristic.to_dict() if best_heuristic else {}

    # Exact reference matching, expressed as a candidate-level baseline: accept
    # only pairs whose references already agree.
    exact_scores = [
        1.0 if row.get("reference_exact", 0.0) > 0 else 0.0 for row in labeled.feature_rows
    ]
    report.baselines["exact_reference_only"] = evaluate_at_threshold(
        exact_scores, labeled.labels, 1.0
    ).to_dict()

    # Amount-and-window rule: the naive approach a spreadsheet would take.
    naive_scores = [
        1.0 if row.get("amount_exact", 0.0) > 0 and row.get("within_window", 0.0) > 0 else 0.0
        for row in labeled.feature_rows
    ]
    report.baselines["amount_and_window_rule"] = evaluate_at_threshold(
        naive_scores, labeled.labels, 1.0
    ).to_dict()

    report.calibration = scorer.reliability(labeled.feature_rows, labeled.labels)
    if calibration:
        report.calibration["global"] = {
            "accept": calibration.accept_threshold,
            "review": calibration.review_threshold,
            "achieved_precision_lower_bound": calibration.achieved_precision_lower_bound,
            "calibration_size": calibration.calibration_size,
            "automation_disabled": calibration.automation_disabled,
        }
        report.calibration["by_relation"] = {
            relation: {
                "accept": outcome.accept_threshold,
                "achieved_precision_lower_bound": outcome.achieved_precision_lower_bound,
                "calibration_size": outcome.calibration_size,
                "positives": outcome.positives,
                "coverage_at_accept": outcome.coverage_at_accept,
                "automation_disabled": outcome.automation_disabled,
            }
            for relation, outcome in scorer.calibration_by_relation.items()
        }
    report.coefficients = scorer.coefficients()

    # Per-corruption breakdown at the production threshold.
    buckets: dict[str, dict[str, int]] = {}
    for label, tags, accepted in zip(
        labeled.labels, labeled.corruptions, accepted_flags, strict=True
    ):
        keys = tags or ["clean"]
        for tag in keys:
            bucket = buckets.setdefault(
                tag, {"candidates": 0, "positives": 0, "selected": 0, "true_positives": 0}
            )
            bucket["candidates"] += 1
            bucket["positives"] += label
            if accepted:
                bucket["selected"] += 1
                bucket["true_positives"] += label
    report.per_corruption = {
        tag: {
            **values,
            "precision": (
                values["true_positives"] / values["selected"] if values["selected"] else None
            ),
            "recall": (
                values["true_positives"] / values["positives"] if values["positives"] else None
            ),
        }
        for tag, values in sorted(buckets.items())
    }
    return report, batch, truth


def run_full_evaluation(
    session: Session,
    *,
    train_spec: DatasetSpec,
    calibration_spec: DatasetSpec,
    test_spec: DatasetSpec,
    workdir: Path,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Fit, calibrate, evaluate and reconcile end to end.

    Three datasets from three different seeds, each with one job: an augmented
    one to fit on, a realistically distributed one to calibrate thresholds on,
    and a third held out entirely for the reported numbers.
    """
    settings = settings or get_settings()
    workdir.mkdir(parents=True, exist_ok=True)

    scorer, train, calibration = train_scorer(
        session,
        spec=train_spec,
        calibration_spec=calibration_spec,
        directory=workdir / "train",
        settings=settings,
    )
    report, test_batch, _truth = evaluate_scorer(
        session, scorer, spec=test_spec, directory=workdir / "test", settings=settings
    )

    pipeline = ReconciliationPipeline(session, settings=settings, scorer=scorer)
    run = pipeline.run(test_batch)

    artifact_dir = settings.artifact_dir / "scorer"
    scorer.save(artifact_dir)

    payload = {
        "training": {
            "fit_dataset": train_spec.name,
            "fit_seed": train_spec.seed,
            "fit_rows": len(train),
            "fit_positives": train.positives,
            "fit_augmented": True,
            "calibration_dataset": calibration_spec.name,
            "calibration_seed": calibration_spec.seed,
            "calibration_rows": len(calibration),
            "calibration_positives": calibration.positives,
            "calibration_distribution": "realistic",
            "test_dataset": test_spec.name,
            "test_seed": test_spec.seed,
        },
        "evaluation": report.to_dict(),
        "reconciliation": run.metrics.to_dict(),
        "artifact_dir": str(artifact_dir),
        "test_batch_id": test_batch.id,
    }
    report_path = settings.artifact_dir / "evaluation_report.json"
    report_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload
