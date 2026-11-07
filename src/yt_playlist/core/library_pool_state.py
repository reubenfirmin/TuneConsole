"""Clicked fallback parameters attached to the complete recommendation pool owner."""
import time
import uuid

from . import genre_identity, recommendation_outputs
from .recommendation_work import finite

LANES = ('wheelhouse', 'explore', 'comfort', 'temporal')
SURFACES = tuple('home_pool:' + lane for lane in LANES)


def validate(surface, value):
    if (surface not in SURFACES or not isinstance(value, dict) or set(value) != {'parameters', 'tracks'}
            or not isinstance(value['tracks'], list)):
        raise ValueError('Unknown clicked library pool owner or fields')
    parameters = value['parameters']
    if (not isinstance(parameters, dict) or set(parameters) != {'lane', 'epoch', 'include_genres', 'avoid_genres'}
            or parameters['lane'] != surface.split(':')[1] or type(parameters['epoch']) is not int
            or not 0 <= parameters['epoch'] < 2 ** 63):
        raise ValueError('Invalid clicked library pool parameters')
    for field in ('include_genres', 'avoid_genres'):
        values = parameters[field]
        if (not isinstance(values, list) or len(values) > 100 or len(set(values)) != len(values)
                or any(not isinstance(value, str) or not value or len(value.encode()) > 1024 for value in values)):
            raise ValueError('Invalid clicked library genre selection')
    return value


def decode(surface, raw):
    if not isinstance(raw, str) or len(raw.encode()) > 128 * 1024 * 1024:
        raise ValueError('Clicked library pool exceeds its publication budget')
    return validate(surface, recommendation_outputs.decode_json(raw))


def prepare(conn, surface, value, at, *, policy=None, input_revision=None, pending_policies=(), capture_only=False):
    from yt_playlist.rec import bundle_storage
    inputs = publication_inputs(surface, value, at, policy=policy, input_revision=input_revision,
                                pending_policies=pending_policies, capture_only=capture_only)
    if inputs is None:
        return None
    bundle, options = inputs
    return bundle_storage.prepare(conn, bundle, None, **options)


def publication_inputs(surface, value, at, *, policy=None, input_revision=None, pending_policies=(), capture_only=False):
    validate(surface, value)
    policies = {track.get('pool_policy') for track in value['tracks']}
    if len(policies) > 1 or (policies and None in policies):
        raise ValueError('Clicked library pool has no coherent recorded policy')
    policy = policy or next(iter(policies), None)
    if policy is None:
        return None
    created = finite(at) if at is not None else None if capture_only else time.time()
    lane = value['parameters']['lane']
    return {'all': {lane: value['tracks']}}, dict(
        external_key='clicked-pool:' + uuid.uuid4().hex, policy_version=policy,
        input_revision=input_revision, created_at=created, pending_policies=pending_policies,
        home_surface=surface, allow_unobserved=capture_only)


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
    from yt_playlist.repos import recommendation_publication
    plan = prepare(conn, surface, value, at)
    if plan is None:
        conn.execute('DELETE FROM recommendation_heads WHERE surface=?', (surface,))
        return
    recommendation_publication.import_plan(conn, plan, plan.created_at,
        before_publish=lambda generation: parameters(conn, generation, value['parameters'], at))
