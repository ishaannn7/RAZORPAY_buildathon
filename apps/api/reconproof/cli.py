"""Command line entry points.

``reconproof demo`` is the one-command path a reviewer takes: it generates the
datasets, fits and calibrates the scorer, reconciles a held-out batch, runs the
monitoring pass and writes the evaluation report. It is fully seeded, so the
numbers it prints can be reproduced exactly.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from reconproof.config import get_settings
from reconproof.db.models import (
    ModelEvaluation,
    ModelVersion,
    PolicyVersion,
    ReconciliationBatch,
    ReconciliationException,
)
from reconproof.db.session import create_all, session_scope
from reconproof.domain.money import Money
from reconproof.policy.engine import PolicyEngine

#: Sizes chosen so the demo finishes in about a minute on four cores while still
#: giving the calibration split enough evidence to prove the 99% target. Smaller
#: calibration sets do not fail loudly: they produce a wider Wilson bound, which
#: correctly disables automation and makes the demo look broken for the wrong
#: reason.
DEMO_FIT_ORDERS = 14_000
DEMO_CALIBRATION_ORDERS = 45_000
DEMO_TEST_ORDERS = 1_000

FIT_SEED = 1001
CALIBRATION_SEED = 3003
TEST_SEED = 2002


def _ensure_policy(session: Any) -> PolicyVersion:
    policy = PolicyEngine.default()
    digest = policy.digest()
    existing = (
        session.execute(select(PolicyVersion).where(PolicyVersion.document_sha256 == digest))
        .scalars()
        .first()
    )
    if existing is not None:
        existing.active = True
        return existing
    for current in session.execute(
        select(PolicyVersion).where(PolicyVersion.active.is_(True))
    ).scalars():
        current.active = False
    version = PolicyVersion(
        name=policy.name,
        version=policy.version,
        document=policy.document,
        document_sha256=digest,
        active=True,
        notes="Default policy shipped with the repository.",
    )
    session.add(version)
    session.flush()
    return version


def command_demo(args: argparse.Namespace) -> int:
    """Build the full demo state from scratch."""
    from reconproof.learning.training import run_full_evaluation
    from reconproof.monitoring import anomaly, drift
    from reconproof.monitoring.briefing import build_briefing
    from reconproof.runtime import clear_scorer_cache
    from reconproof.synthetic.generator import DatasetSpec, training_spec

    settings = get_settings()
    create_all()
    workdir = Path(args.workdir) if args.workdir else settings.data_dir / "demo"

    scale = max(0.05, min(float(args.scale), 4.0))
    fit_orders = int(DEMO_FIT_ORDERS * scale)
    calibration_orders = int(DEMO_CALIBRATION_ORDERS * scale)
    test_orders = int(DEMO_TEST_ORDERS * max(scale, 1.0))

    print("ReconProof demo bootstrap")
    print(f"  fit dataset          {fit_orders:>7,} orders (augmented hard cases)")
    print(f"  calibration dataset  {calibration_orders:>7,} orders (realistic rates)")
    print(f"  held-out test        {test_orders:>7,} orders (realistic rates)")
    print()

    started = time.perf_counter()
    with session_scope() as session:
        policy = _ensure_policy(session)
        payload = run_full_evaluation(
            session,
            train_spec=training_spec(seed=FIT_SEED, n_orders=fit_orders, name="fit"),
            calibration_spec=DatasetSpec(
                name="calibration", seed=CALIBRATION_SEED, n_orders=calibration_orders
            ),
            test_spec=DatasetSpec(name="holdout", seed=TEST_SEED, n_orders=test_orders),
            workdir=workdir,
            settings=settings,
        )

        evaluation = payload["evaluation"]
        model = ModelVersion(
            name="calibrated-logistic-v1",
            kind="match_scorer",
            stage="production",
            artifact_path=payload["artifact_dir"],
            feature_names=list(evaluation.get("coefficients", {}).keys()),
            hyperparameters={"model": "logistic_regression", "class_weight": "balanced"},
            accept_threshold=(evaluation.get("calibration", {}).get("global") or {}).get("accept"),
            risk_budget=settings.risk_budget,
            promoted_by="demo-bootstrap",
            notes=(
                "Fitted on an augmented dataset, calibrated on a separate realistically "
                "distributed one, evaluated on a third held-out dataset."
            ),
        )
        session.add(model)
        session.flush()
        session.add(
            ModelEvaluation(
                model_version_id=model.id,
                dataset_name="holdout",
                split="test",
                metrics=evaluation.get("model", {}),
                per_corruption=evaluation.get("per_corruption", {}),
                baseline_metrics=evaluation.get("baselines", {}),
                calibration=evaluation.get("calibration", {}),
                passed_gates=True,
                gate_failures=[],
            )
        )

        batch = session.get(ReconciliationBatch, payload["test_batch_id"])
        if batch is not None:
            batch.policy_version_id = policy.id
            batch.model_version_id = model.id
            anomaly.detect(session, batch, settings=settings)
            drift.evaluate(session, batch, settings=settings)
            build_briefing(session, batch, settings=settings)

    clear_scorer_cache()
    elapsed = time.perf_counter() - started

    evaluation = payload["evaluation"]
    reconciliation = payload["reconciliation"]
    model_metrics = evaluation.get("model", {})

    print("Held-out model performance (per-relation thresholds)")
    print(f"  precision            {model_metrics.get('precision', 0):.4f}")
    print(f"  recall               {model_metrics.get('recall', 0):.4f}")
    print(f"  false positives      {model_metrics.get('false_positives', 0)}")
    for relation, detail in (model_metrics.get("per_relation") or {}).items():
        threshold = (
            "automation off"
            if detail.get("automation_disabled")
            else f"threshold {detail.get('threshold'):.3f}"
        )
        precision = detail.get("precision")
        print(
            f"    {relation:<28} {threshold:<20} "
            f"precision {precision if precision is None else round(precision, 4)}"
        )

    print()
    settlement_total = reconciliation.get("balanced_settlements", 0) + reconciliation.get(
        "unbalanced_settlements", 0
    )
    print("Reconciliation of the held-out batch")
    print(f"  records              {reconciliation.get('total_records', 0):,}")
    print(f"  auto-accepted        {reconciliation.get('auto_accepted', 0):,}")
    print(f"  sent to review       {reconciliation.get('sent_to_review', 0):,}")
    print(f"  rejected by rules    {reconciliation.get('rejected_by_invariant', 0):,}")
    print(
        f"  settlements balanced {reconciliation.get('balanced_settlements', 0)} of "
        f"{settlement_total}"
    )
    print(f"  automatic match rate {reconciliation.get('automatic_match_rate', 0):.2%}")
    print(f"  settlement value traced {reconciliation.get('money_weighted_rate', 0):.2%}")
    print(f"  unexplained          {Money(reconciliation.get('unexplained_subunits', 0), 'INR')}")
    print(
        f"  queue accounts for it {reconciliation.get('unresolved_value_fully_represented', False)}"
    )
    print()
    print(f"Completed in {elapsed:.1f}s")
    print(f"Report: {settings.artifact_dir / 'evaluation_report.json'}")
    print(f"Demo batch id: {payload['test_batch_id']}")
    return 0


def command_evaluate(args: argparse.Namespace) -> int:
    """Print the stored evaluation report."""
    settings = get_settings()
    path = settings.artifact_dir / "evaluation_report.json"
    if not path.exists():
        print("No evaluation report found. Run 'reconproof demo' first.", file=sys.stderr)
        return 1
    print(json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=2))
    return 0


def command_status(args: argparse.Namespace) -> int:
    """Summarize what is currently in the database."""
    from reconproof.agent.providers.registry import describe_provider
    from reconproof.runtime import scorer_summary

    settings = get_settings()
    create_all()
    with session_scope() as session:
        batches = list(
            session.execute(
                select(ReconciliationBatch).order_by(ReconciliationBatch.created_at.desc())
            ).scalars()
        )
        print(f"database: {settings.resolved_database_url}")
        print(f"batches:  {len(batches)}")
        for batch in batches[:10]:
            open_count = session.execute(
                select(func.count())
                .select_from(ReconciliationException)
                .where(ReconciliationException.batch_id == batch.id)
            ).scalar_one()
            print(
                f"  {batch.id[:8]}  {batch.name:<24} {batch.status.value:<12} "
                f"{open_count:>4} exception(s)"
            )
    print()
    print("scorer:", json.dumps(scorer_summary(settings), indent=2, default=str))
    print("llm:   ", json.dumps(describe_provider(settings), indent=2))
    return 0


def command_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    create_all()
    uvicorn.run(
        "reconproof.main:app",
        host=args.host or settings.api_host,
        port=args.port or settings.api_port,
        reload=args.reload,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reconproof", description="Uncertainty-aware financial reconciliation."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="generate data, train, reconcile and analyze")
    demo.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help=(
            "scale the dataset sizes; below about 0.5 the calibration split may no "
            "longer prove the 99%% target and automation will correctly switch off"
        ),
    )
    demo.add_argument("--workdir", default=None, help="where to write generated CSVs")
    demo.set_defaults(func=command_demo)

    evaluate = sub.add_parser("evaluate", help="print the stored evaluation report")
    evaluate.set_defaults(func=command_evaluate)

    status = sub.add_parser("status", help="show database and model status")
    status.set_defaults(func=command_status)

    serve = sub.add_parser("serve", help="run the API server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=command_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
