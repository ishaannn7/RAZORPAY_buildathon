"""Active-learning store.

Human resolutions are the only source of labels a real deployment has. Each
approved or overridden decision becomes a labelled example, so the model can be
retrained on the cases that actually reached a person rather than on synthetic
data alone.

Two properties matter for this to be honest:

* An override is a *negative* label for whatever the agent recommended, not just
  a positive for what the reviewer chose. Discarding the negative would train the
  model to repeat the mistake it was corrected for.
* Labels are stored, never applied. Retraining produces a challenger that must
  pass the promotion gates and a human sign-off before it governs anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from reconproof.config import Settings, get_settings
from reconproof.db.models import (
    HumanResolution,
    MatchCandidate,
    ReconciliationException,
    TrainingSnapshot,
)

if TYPE_CHECKING:
    pass


@dataclass(slots=True)
class LabelRecord:
    candidate_id: str
    relation: str
    label: int
    features: dict[str, float]
    source: str
    reviewer: str
    exception_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "relation": self.relation,
            "label": self.label,
            "features": self.features,
            "source": self.source,
            "reviewer": self.reviewer,
            "exception_id": self.exception_id,
        }


def label_path(settings: Settings | None = None) -> Any:
    settings = settings or get_settings()
    return settings.artifact_dir / "human_labels.jsonl"


def record_label(
    db: Session,
    *,
    exception: ReconciliationException,
    candidate: MatchCandidate,
    resolution: HumanResolution,
    settings: Settings | None = None,
) -> list[LabelRecord]:
    """Append labels implied by one human resolution.

    Returns every label written, which is one positive for the chosen candidate
    plus one negative for each competing candidate the reviewer implicitly
    rejected.
    """
    settings = settings or get_settings()
    labels: list[LabelRecord] = [
        LabelRecord(
            candidate_id=candidate.id,
            relation=candidate.relation.value,
            label=1,
            features=candidate.features or {},
            source="human_choice",
            reviewer=resolution.reviewer,
            exception_id=exception.id,
        )
    ]

    # Every other candidate for the same subject was rejected by this choice.
    competitors = list(
        db.execute(
            select(MatchCandidate).where(
                MatchCandidate.batch_id == exception.batch_id,
                MatchCandidate.relation == candidate.relation,
                MatchCandidate.id != candidate.id,
                (MatchCandidate.left_record_id == candidate.left_record_id)
                | (MatchCandidate.right_record_id == candidate.right_record_id),
            )
        ).scalars()
    )
    labels.extend(
        LabelRecord(
            candidate_id=other.id,
            relation=other.relation.value,
            label=0,
            features=other.features or {},
            source="implicit_rejection",
            reviewer=resolution.reviewer,
            exception_id=exception.id,
        )
        for other in competitors
    )

    path = label_path(settings)
    with path.open("a", encoding="utf-8") as handle:
        for label in labels:
            handle.write(json.dumps(label.to_dict()) + "\n")
    return labels


def load_labels(settings: Settings | None = None) -> list[LabelRecord]:
    settings = settings or get_settings()
    path = label_path(settings)
    if not path.exists():
        return []
    records: list[LabelRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        records.append(
            LabelRecord(
                candidate_id=payload["candidate_id"],
                relation=payload["relation"],
                label=int(payload["label"]),
                features=payload.get("features", {}),
                source=payload.get("source", "unknown"),
                reviewer=payload.get("reviewer", "unknown"),
                exception_id=payload.get("exception_id", ""),
            )
        )
    return records


def create_snapshot(
    db: Session, *, name: str, settings: Settings | None = None
) -> TrainingSnapshot:
    """Freeze the current label set so a training run is reproducible."""
    settings = settings or get_settings()
    labels = load_labels(settings)
    directory = settings.artifact_dir / "snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(label.to_dict()) for label in labels), encoding="utf-8")
    snapshot = TrainingSnapshot(
        name=name,
        source="human_resolutions",
        row_count=len(labels),
        positive_count=sum(label.label for label in labels),
        human_label_count=len(labels),
        artifact_path=str(path),
        detail={
            "by_relation": _counts_by(labels, lambda label: label.relation),
            "by_source": _counts_by(labels, lambda label: label.source),
        },
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _counts_by(labels: list[LabelRecord], key: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label in labels:
        counts[key(label)] = counts.get(key(label), 0) + 1
    return counts
