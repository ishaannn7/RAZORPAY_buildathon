"""Runtime wiring for the trained scorer and the active policy.

Kept separate from the pipeline so the pipeline stays a pure function of its
inputs and can be tested without touching the filesystem.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from reconproof.config import Settings, get_settings
from reconproof.matching.scoring import CalibratedMatchScorer

logger = structlog.get_logger(__name__)

_scorer_cache: dict[str, CalibratedMatchScorer | None] = {}


def scorer_dir(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.artifact_dir / "scorer"


def load_active_scorer(
    settings: Settings | None = None, *, refresh: bool = False
) -> CalibratedMatchScorer | None:
    """Load the trained scorer, or return ``None`` if none has been trained.

    Returning ``None`` is a supported state, not a failure: the pipeline then
    runs deterministic matching only and routes every probabilistic candidate to
    review. That is the correct behaviour for a fresh clone, and it keeps the
    product usable before anything has been fitted.
    """
    settings = settings or get_settings()
    directory = scorer_dir(settings)
    key = str(directory)
    if not refresh and key in _scorer_cache:
        return _scorer_cache[key]

    if not (directory / "model.pkl").exists():
        logger.info("scorer.absent", directory=str(directory))
        _scorer_cache[key] = None
        return None
    try:
        scorer = CalibratedMatchScorer.load(directory)
    except Exception as exc:
        logger.warning("scorer.load_failed", directory=str(directory), error=str(exc))
        _scorer_cache[key] = None
        return None
    _scorer_cache[key] = scorer
    return scorer


def clear_scorer_cache() -> None:
    _scorer_cache.clear()


def scorer_summary(settings: Settings | None = None) -> dict[str, Any]:
    """Describe the active scorer for the health endpoint and model page."""
    settings = settings or get_settings()
    scorer = load_active_scorer(settings)
    if scorer is None:
        return {
            "trained": False,
            "note": (
                "No scorer has been trained. Deterministic matching still runs; "
                "probabilistic candidates route to review."
            ),
        }
    metadata_path = scorer_dir(settings) / "metadata.json"
    metadata: dict[str, Any] = {}
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    return {
        "trained": True,
        "training_rows": scorer.training_rows,
        "training_positives": scorer.training_positives,
        "feature_count": len(scorer.feature_names),
        "relations_calibrated": sorted(scorer.calibration_by_relation),
        "thresholds": {
            relation: {
                "accept": outcome.accept_threshold,
                "precision_lower_bound": outcome.achieved_precision_lower_bound,
                "calibration_size": outcome.calibration_size,
                "automation_disabled": outcome.automation_disabled,
            }
            for relation, outcome in scorer.calibration_by_relation.items()
        },
        "coefficients": metadata.get("coefficients", {}),
    }
