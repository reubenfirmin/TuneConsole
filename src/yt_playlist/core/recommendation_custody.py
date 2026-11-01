"""Retain publication generations without importing preparation or display consumers."""


def retain(conn, kind, external_key, generation_id):
    if not conn.in_transaction:
        raise RuntimeError('Recommendation evidence requires an outer transaction')
    conn.execute('INSERT INTO recommendation_generation_refs VALUES (?,?,?) ON CONFLICT DO NOTHING',
                 (kind, external_key, generation_id))


def release(conn, kind, external_key):
    if not conn.in_transaction:
        raise RuntimeError('Recommendation evidence retirement requires an outer transaction')
    conn.execute('DELETE FROM recommendation_generation_refs WHERE kind=? AND external_key=?', (kind, external_key))
