"""Numeric agreement uses absolute distance across every provider pair, without bucket artifacts."""
from itertools import permutations

import pytest

from yt_playlist.providers.base import EnrichmentResult, detect_conflicts


@pytest.mark.parametrize("field,values", [
    ("bpm", [120.9, 121.1]), ("energy", [.04, .06]), ("danceability", [.04, .06]),
    ("bpm", [120, 122]), ("energy", [.7, .8]), ("danceability", [.2, .3]),
    ("bpm", ["120.9", 121.1]), ("bpm", [120, 121, 122]),
])
def test_values_within_tolerance_never_conflict(field, values):
    for ordered in permutations(values):
        results = [EnrichmentResult(str(index), {field: value}) for index, value in enumerate(ordered)]
        assert detect_conflicts(results) == {}


@pytest.mark.parametrize("field,values", [
    ("bpm", [120, 122.01]), ("energy", [.7, .8001]), ("danceability", [.2, .3001]),
    ("bpm", [120, 121.5, 123]), ("energy", [.1, .19, .28]),
])
def test_outside_tolerance_or_a_chain_retains_every_candidate(field, values):
    for ordered in permutations(values):
        results = [EnrichmentResult(str(index), {field: value}) for index, value in enumerate(ordered)]
        assert detect_conflicts(results) == {field: [
            {"provider": str(index), "value": value} for index, value in enumerate(ordered)]}


def test_discrete_agreement_and_missing_values_are_preserved():
    results = [EnrichmentResult("a", {"genre": "  ROCK ", "year": "2000", "bpm": None}),
               EnrichmentResult("b", {"genre": "rock", "year": "2001", "bpm": ""}),
               EnrichmentResult("c", {"genre": "Rock", "year": "2000", "bpm": 120})]
    assert detect_conflicts(results) == {"year": [
        {"provider": "a", "value": "2000"}, {"provider": "b", "value": "2001"},
        {"provider": "c", "value": "2000"}]}
