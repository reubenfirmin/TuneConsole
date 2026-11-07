"""Seed isolated derived-reader fixtures without inventing retained observations.

These helpers deliberately bypass account ratings, projection and learning. Use
library.ratings for reconciliation, provenance upgrades and user-action tests.
An existing fixture row is left intact, including its original evidence time.
"""


def _seed(store, key, kind, now, until, reason):
    with store.conn:
        cursor = store.conn.execute(
            'INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at) '
            "VALUES (?,?,?,?,'',?,?) ON CONFLICT(surface,item_key,scope) DO NOTHING",
            ('like' if kind == 'like' else 'sync', key, kind, reason, until, now))
    return cursor.rowcount == 1


def seed_like(store, key, now, provenance='sync'):
    return _seed(store, key, 'like', now, None, provenance)


def seed_dislike(store, key, until, now):
    return _seed(store, key, 'dislike', now, until, None)


def remove_rating(store, key, kind):
    with store.conn:
        store.conn.execute('DELETE FROM rec_feedback WHERE item_key=? AND kind=?', (key, kind))
