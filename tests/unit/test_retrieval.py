import pytest

from docvault.retrieval import calculate_rrf


@pytest.mark.parametrize(
    ("rankings", "expected"),
    [
        # Agreement across semantic and keyword lists outranks a single-list hit.
        (
            [["semantic-only", "shared"], ["keyword-only", "shared"]],
            ["shared", "keyword-only", "semantic-only"],
        ),
        # An empty keyword result still preserves the semantic ranking.
        ([["z", "a"], []], ["z", "a"]),
        # Equal fused scores use stable IDs, independently of list ordering.
        ([["b", "a"], ["a", "b"]], ["a", "b"]),
        ([[], []], []),
    ],
)
def test_rrf_consensus_missing_matches_and_deterministic_ties(rankings, expected):
    assert calculate_rrf(rankings) == expected
    assert calculate_rrf(list(reversed(rankings))) == expected
