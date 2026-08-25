"""Exact subset attribution for aggregate totals.

A settlement reports the *total* refunds it absorbed, never which refunds those
were. Scoring refund-to-settlement pairs independently is close to hopeless: a
refund's amount and date are equally consistent with any batch that has room for
it, and measured precision on that approach was around 18%.

But the aggregate is exact. So the question is not "does this refund look like it
belongs here" but "which subset of the available refunds sums to exactly this
total". That has a definite answer, and three genuinely different outcomes:

* exactly one subset  — the attribution is *determined*, not estimated;
* several subsets     — the data cannot distinguish them, so a human must;
* no subset           — a record is missing or an amount is wrong.

Reporting which of the three occurred is far more useful than a confidence score,
and it is honest in a way a probability over an under-determined problem is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class SubsetOutcome(StrEnum):
    UNIQUE = "unique"
    AMBIGUOUS = "ambiguous"
    NONE = "none"
    SKIPPED_TOO_LARGE = "skipped_too_large"


@dataclass(slots=True)
class SubsetSolution:
    """The result of attributing one aggregate total."""

    outcome: SubsetOutcome
    solutions: list[tuple[str, ...]] = field(default_factory=list)
    target: int = 0
    candidate_count: int = 0
    explored_nodes: int = 0

    @property
    def unique_solution(self) -> tuple[str, ...] | None:
        return self.solutions[0] if self.outcome is SubsetOutcome.UNIQUE else None

    @property
    def involved_ids(self) -> set[str]:
        """Every id appearing in any solution, for building a review queue."""
        return {item for solution in self.solutions for item in solution}


#: Above this many candidates the enumeration is abandoned rather than allowed to
#: dominate a batch run. The case is reported as unresolved, which is the correct
#: outcome: a guess would be worse than an explicit "too ambiguous to decide".
MAX_CANDIDATES = 24

#: Cap on solutions collected. Two is enough to prove ambiguity; more only costs
#: time, though a few are kept so a reviewer can see the competing options.
MAX_SOLUTIONS = 4

#: Guard against pathological search on adversarial inputs.
MAX_NODES = 200_000


def attribute_exact_subset(
    items: list[tuple[str, int]],
    target: int,
    *,
    max_candidates: int = MAX_CANDIDATES,
    max_solutions: int = MAX_SOLUTIONS,
    max_nodes: int = MAX_NODES,
) -> SubsetSolution:
    """Find subsets of *items* whose amounts sum to exactly *target*.

    *items* is a list of ``(id, positive_subunits)``. Amounts are integers, so
    the comparison is exact equality: there is no tolerance in which a real
    break could hide.
    """
    if target == 0:
        # Nothing to attribute. The empty subset is the unique answer, which is
        # meaningfully different from "no answer".
        return SubsetSolution(
            outcome=SubsetOutcome.UNIQUE, solutions=[()], target=0, candidate_count=len(items)
        )

    positive = [(identifier, amount) for identifier, amount in items if amount > 0]
    if not positive:
        return SubsetSolution(outcome=SubsetOutcome.NONE, target=target, candidate_count=0)

    if len(positive) > max_candidates:
        return SubsetSolution(
            outcome=SubsetOutcome.SKIPPED_TOO_LARGE,
            target=target,
            candidate_count=len(positive),
        )

    # Descending order lets the branch-and-bound prune early: once the remaining
    # suffix cannot reach the target, the whole subtree is dead.
    ordered = sorted(positive, key=lambda pair: -pair[1])
    amounts = [amount for _, amount in ordered]
    identifiers = [identifier for identifier, _ in ordered]

    suffix_totals = [0] * (len(amounts) + 1)
    for index in range(len(amounts) - 1, -1, -1):
        suffix_totals[index] = suffix_totals[index + 1] + amounts[index]

    solutions: list[tuple[str, ...]] = []
    nodes = 0
    exhausted = False

    def search(index: int, remaining: int, chosen: list[int]) -> None:
        nonlocal nodes, exhausted
        if exhausted or len(solutions) >= max_solutions:
            return
        nodes += 1
        if nodes > max_nodes:
            exhausted = True
            return
        if remaining == 0:
            solutions.append(tuple(identifiers[position] for position in chosen))
            return
        if index >= len(amounts) or remaining < 0:
            return
        # Unreachable: even taking everything left falls short.
        if suffix_totals[index] < remaining:
            return

        chosen.append(index)
        search(index + 1, remaining - amounts[index], chosen)
        chosen.pop()
        search(index + 1, remaining, chosen)

    search(0, target, [])

    if exhausted and not solutions:
        return SubsetSolution(
            outcome=SubsetOutcome.SKIPPED_TOO_LARGE,
            target=target,
            candidate_count=len(positive),
            explored_nodes=nodes,
        )
    if not solutions:
        return SubsetSolution(
            outcome=SubsetOutcome.NONE,
            target=target,
            candidate_count=len(positive),
            explored_nodes=nodes,
        )
    outcome = SubsetOutcome.UNIQUE if len(solutions) == 1 else SubsetOutcome.AMBIGUOUS
    return SubsetSolution(
        outcome=outcome,
        solutions=solutions,
        target=target,
        candidate_count=len(positive),
        explored_nodes=nodes,
    )
