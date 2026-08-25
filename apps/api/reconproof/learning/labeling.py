"""Ground-truth labeling for candidate links.

Labels come from the generator's manifest, expressed in dedupe-key space so they
survive ingestion without depending on database identifiers. A candidate is
positive only if the exact ``(left, right, relation)`` triple appears in the
truth set: a link that connects the right records under the wrong relation is
still wrong.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from reconproof.domain.entities import MatchRelation
from reconproof.matching.types import ProposedLink


@dataclass(slots=True)
class TruthSet:
    """Ground-truth links keyed by ``(left_key, right_key, relation)``."""

    links: set[tuple[str, str, str]]
    allocations: dict[tuple[str, str, str], int]
    corruptions: dict[str, list[str]]
    groups: dict[str, str]

    @classmethod
    def from_manifest(cls, path: Path) -> TruthSet:
        payload = json.loads(path.read_text(encoding="utf-8"))
        links: set[tuple[str, str, str]] = set()
        allocations: dict[tuple[str, str, str], int] = {}
        for entry in payload["truth_links"]:
            key = (entry["left_key"], entry["right_key"], entry["relation"])
            links.add(key)
            allocations[key] = int(entry["allocated_subunits"])
        return cls(
            links=links,
            allocations=allocations,
            corruptions=payload.get("record_corruptions", {}),
            groups=payload.get("record_groups", {}),
        )

    def label(self, link: ProposedLink) -> int:
        key = (link.left.dedupe_key, link.right.dedupe_key, link.relation.value)
        return 1 if key in self.links else 0

    def expected_allocation(self, link: ProposedLink) -> int | None:
        key = (link.left.dedupe_key, link.right.dedupe_key, link.relation.value)
        return self.allocations.get(key)

    def corruptions_for(self, link: ProposedLink) -> list[str]:
        """Corruptions present on either side of the pair.

        Used to break evaluation down by corruption type, which is how a weak
        spot becomes visible instead of averaging away.
        """
        tags = set(self.corruptions.get(link.left.dedupe_key, []))
        tags.update(self.corruptions.get(link.right.dedupe_key, []))
        return sorted(tags)

    def positives_for_relation(self, relation: MatchRelation) -> int:
        return sum(1 for _, _, name in self.links if name == relation.value)

    @property
    def total_positives(self) -> int:
        return len(self.links)


@dataclass(slots=True)
class LabeledCandidates:
    links: list[ProposedLink]
    labels: list[int]
    corruptions: list[list[str]]

    def __len__(self) -> int:
        return len(self.links)

    @property
    def positives(self) -> int:
        return sum(self.labels)

    def split_temporal(
        self, train_fraction: float = 0.6
    ) -> tuple[LabeledCandidates, LabeledCandidates]:
        """Split by time rather than at random.

        A random split leaks: candidates from the same settlement land on both
        sides, and the model is scored on records it effectively saw. Splitting
        on the target record's timestamp makes calibration measure what it
        claims to — performance on a period the model was not fitted on.
        """
        indexed = sorted(
            range(len(self.links)),
            key=lambda index: (
                self.links[index].right.occurred_at.timestamp()
                if self.links[index].right.occurred_at
                else 0.0
            ),
        )
        cut = int(len(indexed) * train_fraction)
        train_indices, calibration_indices = indexed[:cut], indexed[cut:]
        return self._subset(train_indices), self._subset(calibration_indices)

    def _subset(self, indices: list[int]) -> LabeledCandidates:
        return LabeledCandidates(
            links=[self.links[index] for index in indices],
            labels=[self.labels[index] for index in indices],
            corruptions=[self.corruptions[index] for index in indices],
        )

    @property
    def feature_rows(self) -> list[dict[str, float]]:
        return [link.features for link in self.links]


def label_candidates(links: list[ProposedLink], truth: TruthSet) -> LabeledCandidates:
    return LabeledCandidates(
        links=links,
        labels=[truth.label(link) for link in links],
        corruptions=[truth.corruptions_for(link) for link in links],
    )
