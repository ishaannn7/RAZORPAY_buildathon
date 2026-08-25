"""Globally constrained assignment over the candidate graph.

Scoring each pair independently is not enough. Three bank credits can each look
like the best match for the same settlement, and accepting all three produces a
ledger that is locally plausible and globally impossible.

So one-to-one relations are resolved as an optimal assignment problem rather than
greedily: the solver maximizes total confidence subject to each record being used
at most once. Many-to-one relations (payments fanning into a settlement) are
resolved under an amount capacity constraint instead, because there the limit is
value rather than count.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import networkx as nx
import numpy as np
from scipy.optimize import linear_sum_assignment

from reconproof.domain.entities import MANY_TO_ONE_RELATIONS, MatchMethod, MatchRelation
from reconproof.matching.types import EvidenceDraft, ProposedLink

#: Cost assigned to a non-edge in the padded assignment matrix. Large enough
#: that the solver never prefers it over a real candidate.
FORBIDDEN_COST = 1e6


@dataclass(slots=True)
class AssignmentOutcome:
    selected: list[ProposedLink] = field(default_factory=list)
    #: Candidates the solver rejected because a better global arrangement
    #: existed. Retained because "this was displaced by a stronger claim" is a
    #: reason a reviewer needs to see.
    displaced: list[ProposedLink] = field(default_factory=list)
    contested_groups: int = 0
    #: Pairs whose score was so close that the winner is not meaningfully
    #: better. These are forced to review regardless of threshold.
    ties: list[tuple[ProposedLink, ProposedLink]] = field(default_factory=list)


#: Two candidates within this score gap are treated as indistinguishable.
TIE_EPSILON = 0.02


def _cost(link: ProposedLink) -> float:
    """Convert a score into an assignment cost.

    Negative log turns "maximize the product of confidences" into "minimize a
    sum", which is what the assignment solver needs.
    """
    score = max(min(link.score or 0.0, 1.0 - 1e-9), 1e-9)
    return -float(np.log(score))


def resolve(links: list[ProposedLink], *, minimum_score: float = 0.0) -> AssignmentOutcome:
    """Resolve every relation in *links* into a globally consistent selection."""
    outcome = AssignmentOutcome()
    by_relation: dict[MatchRelation, list[ProposedLink]] = defaultdict(list)
    for link in links:
        if link.score is not None and link.score < minimum_score:
            outcome.displaced.append(link)
            continue
        by_relation[link.relation].append(link)

    for relation, relation_links in by_relation.items():
        if relation in MANY_TO_ONE_RELATIONS:
            partial = _resolve_many_to_one(relation_links)
        else:
            partial = _resolve_one_to_one(relation_links)
        outcome.selected.extend(partial.selected)
        outcome.displaced.extend(partial.displaced)
        outcome.contested_groups += partial.contested_groups
        outcome.ties.extend(partial.ties)

    return outcome


def _components(links: list[ProposedLink]) -> list[list[ProposedLink]]:
    """Split the candidate set into independent sub-problems.

    Records that share no candidate cannot affect each other's assignment, so
    each connected component is solved separately. Without this the matrix would
    be the size of the whole batch and the solve would dominate runtime.
    """
    graph = nx.Graph()
    for index, link in enumerate(links):
        left = ("L", link.left.id)
        right = ("R", link.right.id)
        graph.add_edge(left, right, index=index)

    groups: list[list[ProposedLink]] = []
    for nodes in nx.connected_components(graph):
        subgraph = graph.subgraph(nodes)
        indices = {data["index"] for _, _, data in subgraph.edges(data=True)}
        groups.append([links[index] for index in sorted(indices)])
    return groups


def _resolve_one_to_one(links: list[ProposedLink]) -> AssignmentOutcome:
    outcome = AssignmentOutcome()
    for group in _components(links):
        left_ids = sorted({link.left.id for link in group})
        right_ids = sorted({link.right.id for link in group})

        if len(group) == 1:
            outcome.selected.append(group[0])
            continue

        outcome.contested_groups += 1
        left_index = {record_id: position for position, record_id in enumerate(left_ids)}
        right_index = {record_id: position for position, record_id in enumerate(right_ids)}

        size = max(len(left_ids), len(right_ids))
        matrix = np.full((size, size), FORBIDDEN_COST, dtype=float)
        edge_lookup: dict[tuple[int, int], ProposedLink] = {}
        for link in group:
            row = left_index[link.left.id]
            column = right_index[link.right.id]
            cost = _cost(link)
            # Keep the cheapest edge when duplicates exist for a pair.
            if cost < matrix[row, column]:
                matrix[row, column] = cost
                edge_lookup[(row, column)] = link

        rows, columns = linear_sum_assignment(matrix)
        chosen: set[tuple[int, int]] = set()
        for row, column in zip(rows, columns, strict=True):
            if matrix[row, column] >= FORBIDDEN_COST:
                continue
            chosen.add((row, column))

        best_by_right: dict[int, list[ProposedLink]] = defaultdict(list)
        for (_row, column), link in edge_lookup.items():
            best_by_right[column].append(link)

        for position, link in edge_lookup.items():
            if position in chosen:
                competitors = sorted(
                    (other for other in best_by_right[position[1]] if other is not link),
                    key=lambda candidate: -(candidate.score or 0.0),
                )
                if competitors:
                    runner_up = competitors[0]
                    gap = (link.score or 0.0) - (runner_up.score or 0.0)
                    link.evidence.append(
                        EvidenceDraft(
                            kind="global_assignment",
                            statement=(
                                f"Chosen over {len(competitors)} competing candidate(s); "
                                f"the next best scored {runner_up.score or 0:.3f}."
                            ),
                            supports=gap > TIE_EPSILON,
                            weight=abs(gap),
                            detail={"score_gap": gap, "competitors": len(competitors)},
                        )
                    )
                    if gap <= TIE_EPSILON:
                        # Winning by a hair is not winning. Forcing review here
                        # is what keeps a coin-flip out of the ledger.
                        outcome.ties.append((link, runner_up))
                if link.method is MatchMethod.CALIBRATED_MODEL:
                    link.method = MatchMethod.GLOBAL_ASSIGNMENT
                outcome.selected.append(link)
            else:
                link.evidence.append(
                    EvidenceDraft(
                        kind="global_assignment",
                        statement=(
                            "Displaced by the globally optimal assignment: another record "
                            "has a stronger claim to this counterpart."
                        ),
                        supports=False,
                        weight=1.0,
                    )
                )
                outcome.displaced.append(link)
    return outcome


def _resolve_many_to_one(links: list[ProposedLink]) -> AssignmentOutcome:
    """Fill each target up to its capacity, best candidates first.

    The constraint is value, not count: a settlement can absorb any number of
    payments as long as their total does not exceed its unexplained residual.
    Optimal subset selection here is a knapsack, so this takes the greedy
    descent by confidence and records what it could not fit rather than
    pretending to be optimal.
    """
    outcome = AssignmentOutcome()
    by_target: dict[str, list[ProposedLink]] = defaultdict(list)
    for link in links:
        by_target[link.right.id].append(link)

    for group in by_target.values():
        target = group[0].right
        capacity = abs(target.amount_subunits)
        # Deductions (fees, refunds) do not consume the settlement's positive
        # capacity; they are part of the balance identity checked separately.
        positive = [link for link in group if link.allocated_subunits > 0]
        negative = [link for link in group if link.allocated_subunits <= 0]
        outcome.selected.extend(negative)

        used = 0
        claimed_sources: set[str] = set()
        for link in sorted(positive, key=lambda candidate: -(candidate.score or 0.0)):
            magnitude = abs(link.allocated_subunits)
            if link.left.id in claimed_sources:
                link.evidence.append(
                    EvidenceDraft(
                        kind="capacity",
                        statement="This source record is already allocated to this target.",
                        supports=False,
                    )
                )
                outcome.displaced.append(link)
                continue
            if used + magnitude > capacity:
                link.evidence.append(
                    EvidenceDraft(
                        kind="capacity",
                        statement=(
                            "Rejected: allocating this record would exceed the target's "
                            "remaining value."
                        ),
                        supports=False,
                        detail={"used": used, "capacity": capacity, "requested": magnitude},
                    )
                )
                outcome.displaced.append(link)
                continue
            used += magnitude
            claimed_sources.add(link.left.id)
            outcome.selected.append(link)

        if len(positive) > 1:
            outcome.contested_groups += 1
    return outcome


def build_evidence_graph(links: list[ProposedLink]) -> nx.DiGraph:
    """Build the directed evidence graph for visualization and agent traversal.

    Node and edge attributes are limited to what the UI renders and what the
    agent's read-only tools expose, so the graph cannot become a side channel
    around the tool boundary.
    """
    graph = nx.DiGraph()
    for link in links:
        for record in (link.left, link.right):
            if record.id not in graph:
                graph.add_node(
                    record.id,
                    kind=record.record_kind.value,
                    source=record.source_kind.value,
                    amount_subunits=record.amount_subunits,
                    currency=record.currency,
                    reference=(
                        record.settlement_ref
                        or record.payment_ref
                        or record.order_ref
                        or record.bank_ref
                        or record.external_id
                    ),
                    occurred_at=record.occurred_at.isoformat() if record.occurred_at else None,
                )
        graph.add_edge(
            link.left.id,
            link.right.id,
            relation=link.relation.value,
            allocated_subunits=link.allocated_subunits,
            score=link.score,
            risk=link.risk,
            method=link.method.value,
            blocking=link.blocking_invariants,
        )
    return graph
