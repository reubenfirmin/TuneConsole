"""Project retained account/upload ratings into recording feedback.

Neutral evidence clears only its own contribution. Across retained contributions,
DISLIKE wins over LIKE, which wins over INDIFFERENT. Account availability does not
change that policy: an unread account has not withdrawn its rating.
Callers own the connection transaction and lock; projection never replays learning.
"""
from dataclasses import dataclass


PRIORITY = {"INDIFFERENT": 0, "LIKE": 1, "DISLIKE": 2}


@dataclass(frozen=True)
class Rating:
    status: str
    observed_at: float
    provenance: str = "sync"

    def priority(self):
        return PRIORITY[self.status], self.provenance == 'action', -self.observed_at


def aggregate(conn, keys):
    """Keep exact observations intact and combine only verified recording associations."""
    keys = sorted(set(keys) - {None})
    result = {}
    for offset in range(0, len(keys), 500):
        batch = keys[offset:offset + 500]
        slots = ','.join('?' for _ in batch)
        rows = conn.execute(
            'SELECT r.identity_key,ir.status,ir.source,ir.observed_at FROM item_ratings ir '
            'JOIN youtube_items y ON y.video_id=ir.video_id '
            'JOIN recordings r ON r.id=y.recording_id '
            f'WHERE r.identity_key IN ({slots})', batch)
        for row in rows:
            candidate = Rating(row['status'], row['observed_at'],
                               'action' if row['source'] in ('api', 'action') else 'sync')
            key = row['identity_key']
            # Prefer real action evidence, then its earliest observation. Neither encounter
            # order nor another account's neutral snapshot refreshes an action's timestamp.
            if key not in result or candidate.priority() > result[key].priority():
                result[key] = candidate
    return result


def _earliest(rows):
    """Merge established evidence by age, then expiry, never by encounter order."""
    return min(rows, key=lambda row: (row['created_at'] is None, row['created_at'] or 0,
                                     row['until'] is None, row['until'] or 0,
                                     row['item_key']), default=None)


def _preserve_guard(conn, key, prior_rows):
    # Graduation belongs to the retained contribution even when a dislike masks
    # it, or a destination feedback row already exists. Never re-arm learning.
    guard = _earliest([row for row in prior_rows if row['kind'] == 'like_grad'])
    if guard is not None:
        conn.execute(
            'INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at) '
            "VALUES ('like_grad',?,'like_grad',?, '',NULL,?) "
            'ON CONFLICT(surface,item_key,scope) DO UPDATE SET '
            'created_at=excluded.created_at,reason=excluded.reason '
            'WHERE rec_feedback.created_at IS NULL OR excluded.created_at<rec_feedback.created_at',
            (key, guard['reason'], guard['created_at']))


def apply(conn, states, *, dislike_seconds=None, prior_feedback=None):
    """Apply already-combined states and return newly inserted likes for graduation.

    Association corrections pass prior feedback by destination and no dislike duration: they
    relocate established feedback, preserving its age/expiry and graduation guard,
    rather than treating an identity correction as a new user action.
    """
    new_likes = {}
    for key, rating in states.items():
        if not key or rating.status not in PRIORITY:
            continue
        prior_rows = prior_feedback.get(key, ()) if prior_feedback else ()
        if dislike_seconds is None:
            _preserve_guard(conn, key, prior_rows)
        kind = rating.status.lower()
        conn.execute("DELETE FROM rec_feedback WHERE item_key=? AND kind IN ('like','dislike') "
                     "AND kind<>?", (key, kind))
        if rating.status == 'INDIFFERENT':
            continue
        surface = 'like' if kind == 'like' else 'sync'
        existing = conn.execute(
            "SELECT * FROM rec_feedback WHERE surface=? AND item_key=? AND scope='' AND kind=?",
            (surface, key, kind)).fetchone()
        donor = _earliest([row for row in prior_rows if row['kind'] == kind] +
                          ([existing] if existing else []))
        if existing:
            if dislike_seconds is None:
                # A verified merge keeps the earliest established same-polarity
                # evidence and its original expiry instead of renewing suppression.
                conn.execute("UPDATE rec_feedback SET created_at=?,until=? "
                             "WHERE surface=? AND item_key=? AND scope=''",
                             (donor['created_at'], donor['until'], surface, key))
            if kind == 'like' and rating.provenance == 'action':
                conn.execute("UPDATE rec_feedback SET reason='action' WHERE surface='like' "
                             "AND item_key=? AND scope=''", (key,))
            continue
        if dislike_seconds is None and (not prior_rows or (kind == 'dislike' and donor is None)):
            # Unresolved/pending ratings have not produced model feedback yet. Let their
            # normal ingestion owner resolve and learn them, even after an association change.
            continue
        created_at = donor['created_at'] if donor else rating.observed_at
        until = (donor['until'] if donor else rating.observed_at + dislike_seconds
                 ) if kind == 'dislike' else None
        conn.execute(
            'INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at) '
            "VALUES (?,?,?,?,'',?,?)",
            (surface, key, kind, rating.provenance if kind == 'like' else None, until, created_at))
        if kind == 'like':
            new_likes[key] = Rating('LIKE', created_at, rating.provenance)
    return new_likes


def feedback_rows(conn, keys):
    keys = sorted(set(keys) - {None})
    result = []
    for offset in range(0, len(keys), 500):
        batch = keys[offset:offset + 500]
        slots = ','.join('?' for _ in batch)
        result.extend(dict(row) for row in conn.execute(
            f"SELECT * FROM rec_feedback WHERE item_key IN ({slots}) "
            "AND kind IN ('like','dislike','like_grad')", batch))
    return sorted(result, key=lambda row: (row['created_at'] or 0, row['item_key']))
