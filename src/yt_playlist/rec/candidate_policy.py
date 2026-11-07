"""Detached policy inputs for bounded recommendation queries."""
from dataclasses import dataclass

# SQL executes these domain-owned projections over persisted aggregates. Keeping
# the order in the query avoids loading a whole library merely to rank a page.
COMFORT_ORDER = 'p.c * min(1.0, (:now - p.last) / :full_secs) DESC, p.last ASC'
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
