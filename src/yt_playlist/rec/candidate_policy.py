"""Detached policy inputs for bounded recommendation queries."""
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Protocol, TypeVar

Candidate = TypeVar('Candidate')


@dataclass(frozen=True)
class SelectionInputs:
    """Shared selection contract for confirmed version policy (#296).

    Memberships use recording keys and independent confirmed group ids. Destination
    exclusions retain recording identity. Policy carries explicit playlist/recipe
    choices; revision identifies the captured memberships and policy together.
    The policy implementation owns group competition and multiplicity, without
    querying storage, matching sources or combining recording feedback.
    """
    memberships: Mapping[str, frozenset[str]]
    destination_exclusions: frozenset[str]
    playlist_policy: Mapping[str, object]
    revision: str


class SelectionPolicy(Protocol[Candidate]):
    def __call__(self, candidates: Sequence[Candidate], inputs: SelectionInputs) -> list[Candidate]: ...


def select_candidates(candidates: Sequence[Candidate], inputs: SelectionInputs, *,
                      policy: SelectionPolicy[Candidate]) -> list[Candidate]:
    """Run a detached policy against one captured revision; never acquire missing facts."""
    return policy(candidates, inputs)

# SQL executes these domain-owned projections over persisted aggregates. Keeping
# the order in the query avoids loading a whole library merely to rank a page.
COMFORT_ORDER = 'COALESCE(h.plays,0) DESC,h.last_played,n.key'
ROTATION_ORDER = 'sp DESC, plays ASC, key'
DEEP_CUT_ORDER = 'atot DESC, plays ASC, key'
PLAYLIST_ORDER = '(sa*:artist_weight+cooc) DESC, cooc DESC, key'


@dataclass(frozen=True)
class RevisitCriteria:
    month: float
    half_year: float
    repeated_plays: int
    long_rest_plays: int


def revisit_criteria(now):
    return RevisitCriteria(now - 30 * 86400, now - 183 * 86400, 2, 20)


def comfort_recency_seconds(days):
    return max(1., days * 86400.)


def comfort_criteria(now, minimum_plays, days):
    cutoff = now - comfort_recency_seconds(days)
    return RevisitCriteria(cutoff, cutoff, minimum_plays, 20)
