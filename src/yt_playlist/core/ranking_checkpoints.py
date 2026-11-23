"""Recommendation preparation admission, independent of display cache."""
from math import isfinite
from .worker_settings import read, save

EXPIRY = 'rec_candidates_expire_at'
CHECKED_AT = 'rec_candidates_checked_at'
CHECKED_GROUP = 'rec_candidates_checked_group'
COLUMNS = {
    'rec_candidate_preferences': 'ranking_fingerprint',
    EXPIRY: 'ranking_expires_at',
    CHECKED_AT: 'ranking_checked_at',
    CHECKED_GROUP: 'ranking_checked_group',
}
KEYS = frozenset(COLUMNS)


def get(conn, key, default=None):
    return read(conn, key, default)


def put(conn, values):
    for key, value in values.items():
        if key in (EXPIRY, CHECKED_AT):
            value = None if value in (None, '') else float(value)
            if value is not None and not isfinite(value):
                raise ValueError('Candidate checkpoint time must be finite')
        save(conn, key, str(value) if value is not None else None)
