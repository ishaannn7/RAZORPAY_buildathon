"""Exact subset attribution.

The three outcomes carry different meanings and must never be confused: a unique
subset is a determination, several subsets is a proven ambiguity, and no subset
means something is missing. Collapsing any two of those into "no match" would
throw away the most useful thing the stage produces.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from reconproof.matching.subsetsum import (
    MAX_CANDIDATES,
    SubsetOutcome,
    attribute_exact_subset,
)


class TestOutcomes:
    def test_unique_subset(self) -> None:
        result = attribute_exact_subset([("a", 500), ("b", 300), ("c", 250)], 800)
        assert result.outcome is SubsetOutcome.UNIQUE
        assert set(result.unique_solution or ()) == {"a", "b"}

    def test_ambiguous_when_two_items_share_an_amount(self) -> None:
        result = attribute_exact_subset([("a", 500), ("b", 500)], 500)
        assert result.outcome is SubsetOutcome.AMBIGUOUS
        assert len(result.solutions) == 2
        assert result.involved_ids == {"a", "b"}

    def test_ambiguous_when_different_combinations_tie(self) -> None:
        # 300+200 and 500 both reach 500.
        result = attribute_exact_subset([("a", 500), ("b", 300), ("c", 200)], 500)
        assert result.outcome is SubsetOutcome.AMBIGUOUS

    def test_no_subset(self) -> None:
        result = attribute_exact_subset([("a", 500), ("b", 300)], 777)
        assert result.outcome is SubsetOutcome.NONE
        assert result.solutions == []

    def test_zero_target_is_uniquely_the_empty_set(self) -> None:
        # Distinct from NONE: a settlement reporting no refunds is fully
        # explained, not unexplainable.
        result = attribute_exact_subset([("a", 500)], 0)
        assert result.outcome is SubsetOutcome.UNIQUE
        assert result.unique_solution == ()

    def test_single_exact_item(self) -> None:
        result = attribute_exact_subset([("a", 4500)], 4500)
        assert result.outcome is SubsetOutcome.UNIQUE

    def test_empty_pool_with_nonzero_target(self) -> None:
        assert attribute_exact_subset([], 100).outcome is SubsetOutcome.NONE

    def test_ignores_non_positive_amounts(self) -> None:
        result = attribute_exact_subset([("a", 500), ("b", 0), ("c", -200)], 500)
        assert result.outcome is SubsetOutcome.UNIQUE
        assert result.unique_solution == ("a",)


class TestGuards:
    def test_oversized_pool_is_skipped_not_guessed(self) -> None:
        items = [(f"r{index}", index + 1) for index in range(MAX_CANDIDATES + 5)]
        result = attribute_exact_subset(items, 10)
        assert result.outcome is SubsetOutcome.SKIPPED_TOO_LARGE
        assert result.solutions == []

    def test_node_budget_terminates(self) -> None:
        # A pool of equal amounts is the worst case for enumeration; the search
        # must return rather than run away.
        items = [(f"r{index}", 1000) for index in range(MAX_CANDIDATES)]
        result = attribute_exact_subset(items, 12000, max_nodes=500)
        assert result.outcome in {
            SubsetOutcome.AMBIGUOUS,
            SubsetOutcome.SKIPPED_TOO_LARGE,
            SubsetOutcome.NONE,
        }


class TestProperties:
    @given(
        amounts=st.lists(st.integers(min_value=1, max_value=50_000), min_size=1, max_size=10),
        mask=st.lists(st.booleans(), min_size=1, max_size=10),
    )
    @settings(max_examples=200, deadline=None)
    def test_constructed_subset_is_always_found(self, amounts: list[int], mask: list[bool]) -> None:
        """A subset that genuinely sums to the target must never be missed.

        Recall failures here would silently push correct attributions into the
        exception queue, so this is the property that matters most.
        """
        size = min(len(amounts), len(mask))
        amounts, mask = amounts[:size], mask[:size]
        if not any(mask):
            mask[0] = True
        items = [(f"r{index}", amount) for index, amount in enumerate(amounts)]
        target = sum(amount for amount, keep in zip(amounts, mask, strict=True) if keep)

        result = attribute_exact_subset(items, target)
        assert result.outcome in {SubsetOutcome.UNIQUE, SubsetOutcome.AMBIGUOUS}
        # Every returned solution must actually sum to the target.
        lookup = dict(items)
        for solution in result.solutions:
            assert sum(lookup[identifier] for identifier in solution) == target

    @given(amounts=st.lists(st.integers(min_value=1, max_value=1000), min_size=1, max_size=8))
    @settings(max_examples=100, deadline=None)
    def test_unreachable_target_is_never_claimed(self, amounts: list[int]) -> None:
        items = [(f"r{index}", amount) for index, amount in enumerate(amounts)]
        # One more than everything combined can never be reached.
        result = attribute_exact_subset(items, sum(amounts) + 1)
        assert result.outcome is SubsetOutcome.NONE

    @given(amounts=st.lists(st.integers(min_value=1, max_value=5000), min_size=2, max_size=9))
    @settings(max_examples=100, deadline=None)
    def test_full_pool_total_is_reachable(self, amounts: list[int]) -> None:
        items = [(f"r{index}", amount) for index, amount in enumerate(amounts)]
        result = attribute_exact_subset(items, sum(amounts))
        assert result.outcome in {SubsetOutcome.UNIQUE, SubsetOutcome.AMBIGUOUS}
