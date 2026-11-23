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


def has_upload_rating(conn, video_id):
    return conn.execute(
        "SELECT 1 FROM item_ratings WHERE video_id=? AND status IN ('LIKE','DISLIKE') LIMIT 1",
        (video_id,),
    ).fetchone() is not None


def rating_rows(conn, keys):
    """Read the exact uploads behind each recording's rating."""
    keys = sorted(set(keys) - {None})
    result = []
    for offset in range(0, len(keys), 500):
        batch = keys[offset:offset + 500]
        slots = ','.join('?' for _ in batch)
        result.extend(dict(row) for row in conn.execute(
            'SELECT r.identity_key,ir.video_id,ir.status,ir.source,ir.observed_at,'
            "f.kind='unban' unbanned,f.created_at unbanned_at FROM item_ratings ir "
            'JOIN track_sources y ON y.video_id=ir.video_id '
            'JOIN recordings r ON r.id=y.recording_id '
            "LEFT JOIN rec_feedback f ON f.item_key=r.identity_key AND f.surface='rating_override' "
            "AND f.kind='unban' AND f.scope='' "
            f'WHERE r.identity_key IN ({slots})', batch))
    return result


def observed_rating(row):
    status = row['status']
    if status == 'DISLIKE' and row['unbanned'] and (row['source'] != 'api' or
            row['unbanned_at'] is not None and row['observed_at'] <= row['unbanned_at']):
        status = 'INDIFFERENT'
    return Rating(status, row['observed_at'], 'action' if row['source'] in ('api', 'action') else 'sync')


def _feedback_from_observation(row, observed_at):
    at = max(row['created_at'], observed_at) if row['created_at'] is not None else observed_at
    duration = row['until'] - row['created_at'] if row['until'] is not None and row['created_at'] is not None else None
    return dict(row, created_at=at, until=at + duration if duration is not None else row['until'])


def correction_feedback(feedback, ratings, video, old_key, new_key, *, merge_recordings):
    """Split an upload's projected feedback from the recording it leaves."""
    if merge_recordings:
        return {old_key: [], new_key: feedback}
    retained_at, moved_at = {}, {}
    for row in ratings:
        if row['identity_key'] == old_key:
            rating = observed_rating(row)
            ages = moved_at if row['video_id'] == video else retained_at
            kind = rating.status.lower()
            ages[kind] = min(ages.get(kind, rating.observed_at), rating.observed_at)

    retained, target = [], []
    source_feedback = False
    for row in feedback:
        if row['item_key'] != old_key:
            target.append(row)
            continue
        source_feedback = True
        kind = 'like' if row['kind'] == 'like_grad' else row['kind']
        moved, stays = moved_at.get(kind), retained_at.get(kind)
        if moved is None:
            retained.append(row)
        elif stays is not None and stays < moved:
            retained.append(row)
            target.append(_feedback_from_observation(row, moved))
        else:
            target.append(row)
            if stays is not None and row['kind'] != 'like_grad':
                retained.append(dict(_feedback_from_observation(row, stays), replace_age=True))
    if source_feedback:
        # A retained Like can become visible when the Dislike that masked it moves.
        for rows, dates in ((retained, retained_at), (target, moved_at)):
            if 'like' in dates and not any(row['kind'] == 'like' for row in rows):
                rows.append(dict(item_key=old_key, kind='like', created_at=dates['like'], until=None))
    return {old_key: retained, new_key: target}


def _earliest(rows):
    """Merge established evidence by age, then expiry, never by encounter order."""
    return min(rows, key=lambda row: (row['created_at'] is None, row['created_at'] or 0,
                                     row['until'] is None, row['until'] or 0,
                                     row['item_key']), default=None)


def _association_donor(rows, kind, current):
    prior = [row for row in rows if row['kind'] == kind]
    if current and not any(row.get('replace_age') for row in prior):
        prior.append(current)
    return _earliest(prior)


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
        donor = _association_donor(prior_rows, kind, existing)
        if existing:
            if dislike_seconds is None:
                # A verified merge keeps the earliest established same-polarity
                # evidence and its original expiry instead of renewing suppression.
                reason = rating.provenance if kind == 'like' else existing['reason']
                conn.execute("UPDATE rec_feedback SET created_at=?,until=?,reason=? "
                             "WHERE surface=? AND item_key=? AND scope=''",
                             (donor['created_at'], donor['until'], reason, surface, key))
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


def feedback_changes(states, rows, *, dislike_seconds):
    """Project ordinary, already-aggregated rating observations without SQL."""
    existing = {(row['surface'], row['item_key'], row['kind']): row for row in rows if row['scope'] == ''}
    deleted, actions, inserted, likes = [], [], [], {}
    for key, rating in states.items():
        if not key or rating.status not in PRIORITY:
            continue
        kind = rating.status.lower()
        deleted.append((key, kind))
        if rating.status == 'INDIFFERENT':
            continue
        surface = 'like' if kind == 'like' else 'sync'
        if (surface, key, kind) in existing:
            if kind == 'like' and rating.provenance == 'action':
                actions.append((key,))
            continue
        until = rating.observed_at + dislike_seconds if kind == 'dislike' else None
        inserted.append((surface, key, kind, rating.provenance if kind == 'like' else None, until, rating.observed_at))
        if kind == 'like':
            likes[key] = Rating('LIKE', rating.observed_at, rating.provenance)
    return deleted, actions, inserted, likes


def association_changes(states, rows, prior_feedback):
    """Relocate established feedback without renewing expiry or replaying learning."""
    existing = {(row['surface'], row['item_key'], row['kind']): row for row in rows if row['scope'] == ''}
    deleted, actions, inserted, ages, guards = [], [], [], [], []
    for key, rating in states.items():
        if not key or rating.status not in PRIORITY:
            continue
        prior = prior_feedback.get(key, ())
        guard = _earliest([row for row in prior if row['kind'] == 'like_grad'])
        if guard is not None:
            guards.append((key, guard['reason'], guard['created_at']))
        kind = rating.status.lower()
        deleted.append((key, kind))
        if rating.status == 'INDIFFERENT':
            continue
        surface = 'like' if kind == 'like' else 'sync'
        current = existing.get((surface, key, kind))
        donor = _association_donor(prior, kind, current)
        if current:
            reason = rating.provenance if kind == 'like' else current['reason']
            ages.append((donor['created_at'], donor['until'], reason, surface, key))
        elif prior and (kind != 'dislike' or donor is not None):
            inserted.append((surface, key, kind, rating.provenance if kind == 'like' else None,
                donor['until'] if kind == 'dislike' else None, donor['created_at'] if donor else rating.observed_at))
    return deleted, actions, inserted, ages, guards


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
