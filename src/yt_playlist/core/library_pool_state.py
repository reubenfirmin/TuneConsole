"""Clicked fallback parameters attached to the complete recommendation pool owner."""
from . import genre_identity
from .clicked_pool_policy import LANES, SURFACES, decode, publication_inputs, validate


def prepare(conn, surface, value, at, *, policy=None, input_revision=None, pending_policies=(), capture_only=False):
    from yt_playlist.rec import bundle_storage
    inputs = publication_inputs(surface, value, at, policy=policy, input_revision=input_revision,
                                pending_policies=pending_policies, capture_only=capture_only)
    if inputs is None:
        return None
    bundle, options = inputs
    return bundle_storage.prepare(conn, bundle, None, **options)


def parameters(conn, generation, value, at):
    if not conn.in_transaction:
        raise ValueError('Clicked pool parameters require their publication transaction')
    conn.execute('INSERT INTO clicked_library_pools VALUES (?,?,?,?)',
                 (generation, value['lane'], value['epoch'], at))
    for field, kind in (('include_genres', 'include'), ('avoid_genres', 'avoid')):
        for position, raw in enumerate(value[field]):
            family = conn.execute('SELECT key FROM genre_family WHERE key=?', (raw,)).fetchone()
            genre, reason = (None, None) if family else genre_identity.resolve(conn, raw.removeprefix('other:'))
            conn.execute('INSERT INTO clicked_library_genres VALUES (?,?,?,?,?,?,?)',
                (generation, kind, position, raw, family[0] if family else None, genre, reason))


def read_parameters(conn, surface):
    row = conn.execute('SELECT p.generation_id,p.lane,p.epoch FROM recommendation_heads h '
        'JOIN clicked_library_pools p ON p.generation_id=h.generation_id WHERE h.surface=?', (surface,)).fetchone()
    if row is None:
        return None
    result = {'lane': row[1], 'epoch': row[2]}
    for field, kind in (('include_genres', 'include'), ('avoid_genres', 'avoid')):
        result[field] = [entry[0] for entry in conn.execute('SELECT raw_key FROM clicked_library_genres '
            'WHERE generation_id=? AND kind=? ORDER BY position', (row[0], kind))]
    return result


def read(store, surface):
    selected = read_parameters(store.conn, surface)
    if selected is None:
        return None
    generation = store.recommendations.head(surface)['id']
    tracks, cursor = [], -1
    while True:
        page = store.recommendations.candidate_page(surface, 'all', selected['lane'],
            generation=generation, after=cursor, limit=512)
        tracks.extend(page['tracks'])
        if page['exhausted']:
            return {'parameters': selected, 'tracks': tracks}
        cursor = page['after']


def write(conn, surface, value, at):
    from yt_playlist.controllers import recommendation_import
    plan = prepare(conn, surface, value, at)
    if plan is None:
        conn.execute('DELETE FROM recommendation_heads WHERE surface=?', (surface,))
        return
    recommendation_import.import_plan(conn, plan, plan.created_at,
        before_publish=lambda generation: parameters(conn, generation, value['parameters'], at))
