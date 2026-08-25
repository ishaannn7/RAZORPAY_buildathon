"""Shared types passed between matching stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from reconproof.accounting.invariants import AllocationProposal, InvariantResult
from reconproof.domain.entities import MatchMethod, MatchRelation

if TYPE_CHECKING:
    from reconproof.db.models import SourceRecord


@dataclass(slots=True)
class EvidenceDraft:
    """A citable fact, not yet persisted.

    ``supports=False`` records evidence *against* a link. Keeping contrary
    evidence is deliberate: a reviewer needs to see what argues the other way,
    and an explanation that only lists supporting facts is not an explanation.
    """

    kind: str
    statement: str
    supports: bool = True
    weight: float = 0.0
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ProposedLink:
    """A candidate link travelling through the pipeline."""

    left: SourceRecord
    right: SourceRecord
    relation: MatchRelation
    allocated_subunits: int
    method: MatchMethod
    generator: str
    features: dict[str, float] = field(default_factory=dict)
    score: float | None = None
    risk: float | None = None
    evidence: list[EvidenceDraft] = field(default_factory=list)
    invariants: list[InvariantResult] = field(default_factory=list)
    #: True when the stage that produced this link already reached a definitive
    #: answer, so the statistical scorer must not overwrite it. Exact subset-sum
    #: attribution sets this: "these refunds are the only combination that sums
    #: to the total" is arithmetic, and "several combinations do" is a proven
    #: ambiguity. A model probability on top of either would replace a fact with
    #: an estimate.
    scoring_final: bool = False

    @property
    def pair_key(self) -> tuple[str, str, MatchRelation]:
        return (self.left.id, self.right.id, self.relation)

    def to_proposal(self) -> AllocationProposal:
        return AllocationProposal(
            left=self.left,
            right=self.right,
            relation=self.relation,
            allocated_subunits=self.allocated_subunits,
        )

    @property
    def blocking_invariants(self) -> list[str]:
        return [r.name for r in self.invariants if not r.passed and not r.advisory]

    @property
    def advisory_invariants(self) -> list[str]:
        return [r.name for r in self.invariants if not r.passed and r.advisory]

    @property
    def passed_invariants(self) -> list[str]:
        return [r.name for r in self.invariants if r.passed]
