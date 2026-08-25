"""Model registry and promotion endpoints.

Promotion is deliberately a two-step gate: a challenger must pass the policy's
quantitative gates *and* be approved by a named human. Automatic promotion on
metrics alone would mean the system could change the rules governing its own
automation without anyone deciding to let it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from reconproof.api.schemas import ModelVersionSummary
from reconproof.audit.log import record as audit_record
from reconproof.config import Settings, get_settings
from reconproof.db.models import ModelEvaluation, ModelVersion
from reconproof.db.session import get_db
from reconproof.domain.entities import Actor, AuditAction
from reconproof.learning.store import load_labels
from reconproof.policy.engine import PolicyEngine
from reconproof.runtime import scorer_summary

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=list[ModelVersionSummary])
def list_models(db: Annotated[Session, Depends(get_db)]) -> list[ModelVersionSummary]:
    versions = list(
        db.execute(select(ModelVersion).order_by(ModelVersion.created_at.desc())).scalars()
    )
    summaries: list[ModelVersionSummary] = []
    for version in versions:
        summary = ModelVersionSummary.model_validate(version)
        evaluation = (
            db.execute(
                select(ModelEvaluation)
                .where(ModelEvaluation.model_version_id == version.id)
                .order_by(ModelEvaluation.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if evaluation is None:
            summaries.append(summary)
            continue
        metrics = evaluation.metrics or {}
        summaries.append(
            summary.model_copy(
                update={
                    "gates_passed": evaluation.passed_gates,
                    "gate_failures": list(evaluation.gate_failures or []),
                    "precision_lower_bound": metrics.get("precision_lower_bound"),
                    "coverage": metrics.get("coverage"),
                }
            )
        )
    return summaries


@router.get("/active")
def active_model(config: Annotated[Settings, Depends(get_settings)]) -> dict[str, Any]:
    """Describe the scorer currently governing automation."""
    return scorer_summary(config)


@router.get("/labels")
def label_stats(config: Annotated[Settings, Depends(get_settings)]) -> dict[str, Any]:
    """Human-resolution labels accumulated so far.

    This is the active-learning input: every reviewer decision becomes a
    labelled example, including the negatives implied by choosing one candidate
    over its competitors.
    """
    labels = load_labels(config)
    by_relation: dict[str, dict[str, int]] = {}
    for label in labels:
        bucket = by_relation.setdefault(label.relation, {"positive": 0, "negative": 0})
        bucket["positive" if label.label else "negative"] += 1
    return {
        "total": len(labels),
        "positive": sum(label.label for label in labels),
        "negative": sum(1 for label in labels if not label.label),
        "by_relation": by_relation,
        "by_source": {
            source: sum(1 for label in labels if label.source == source)
            for source in sorted({label.source for label in labels})
        },
        "note": (
            "Labels are stored, never applied automatically. Retraining produces a "
            "challenger that must pass the promotion gates and a human sign-off."
        ),
    }


@router.post("/{model_id}/evaluate")
def evaluate_model(
    model_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """Check a challenger against the policy's promotion gates."""
    challenger = db.get(ModelVersion, model_id)
    if challenger is None:
        raise HTTPException(status_code=404, detail=f"model {model_id} not found")

    evaluation = (
        db.execute(
            select(ModelEvaluation)
            .where(ModelEvaluation.model_version_id == challenger.id)
            .order_by(ModelEvaluation.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if evaluation is None:
        raise HTTPException(
            status_code=400,
            detail="this model has no evaluation on record; it cannot be assessed",
        )

    incumbent = (
        db.execute(select(ModelVersion).where(ModelVersion.stage == "production").limit(1))
        .scalars()
        .first()
    )
    incumbent_metrics: dict[str, Any] = {}
    if incumbent is not None:
        incumbent_evaluation = (
            db.execute(
                select(ModelEvaluation)
                .where(ModelEvaluation.model_version_id == incumbent.id)
                .order_by(ModelEvaluation.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if incumbent_evaluation:
            incumbent_metrics = incumbent_evaluation.metrics or {}

    policy = PolicyEngine.default()
    passed, failures = policy.evaluate_promotion(
        evaluation.metrics or {}, incumbent_metrics or None
    )
    evaluation.passed_gates = passed
    evaluation.gate_failures = failures
    db.flush()
    db.commit()
    return {
        "model_id": challenger.id,
        "passed_gates": passed,
        "failures": failures,
        "requires_human_approval": policy.document["model_promotion"]["requires_human_approval"],
        "metrics": evaluation.metrics,
        "incumbent_metrics": incumbent_metrics,
    }


@router.post("/{model_id}/promote", response_model=ModelVersionSummary)
def promote_model(
    model_id: str,
    db: Annotated[Session, Depends(get_db)],
    approved_by: Annotated[str, Query(min_length=1, max_length=120)],
) -> ModelVersionSummary:
    """Promote a challenger. Requires passing gates and a named approver."""
    challenger = db.get(ModelVersion, model_id)
    if challenger is None:
        raise HTTPException(status_code=404, detail=f"model {model_id} not found")
    if challenger.stage == "production":
        raise HTTPException(status_code=409, detail="model is already in production")

    evaluation = (
        db.execute(
            select(ModelEvaluation)
            .where(ModelEvaluation.model_version_id == challenger.id)
            .order_by(ModelEvaluation.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if evaluation is None or not evaluation.passed_gates:
        failures = evaluation.gate_failures if evaluation else ["no evaluation on record"]
        audit_record(
            db,
            action=AuditAction.MODEL_PROMOTION_REJECTED,
            actor=Actor.HUMAN,
            actor_detail=approved_by,
            model_version_id=challenger.id,
            detail={"failures": failures},
            message=f"Promotion of {challenger.name} refused: gates not passed",
        )
        db.commit()
        raise HTTPException(
            status_code=409,
            detail=f"model has not passed the promotion gates: {failures}",
        )

    from datetime import UTC, datetime

    for current in db.execute(
        select(ModelVersion).where(ModelVersion.stage == "production")
    ).scalars():
        current.stage = "retired"

    challenger.stage = "production"
    challenger.promoted_at = datetime.now(UTC)
    challenger.promoted_by = approved_by
    audit_record(
        db,
        action=AuditAction.MODEL_PROMOTION_APPROVED,
        actor=Actor.HUMAN,
        actor_detail=approved_by,
        model_version_id=challenger.id,
        detail={"metrics": evaluation.metrics},
        message=f"{approved_by} promoted model {challenger.name} to production",
    )
    db.commit()
    return ModelVersionSummary.model_validate(challenger)
