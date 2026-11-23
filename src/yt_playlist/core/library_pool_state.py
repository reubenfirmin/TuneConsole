"""Clicked fallback parameters attached to the complete recommendation pool owner."""
import json

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
    changed = conn.execute('UPDATE recommendation_generations SET clicked_lane=?,clicked_epoch=?,clicked_built_at=?,'
                           'clicked_include_genres=?,clicked_avoid_genres=? WHERE id=?',
                           (value['lane'], value['epoch'], at, json.dumps(value['include_genres']),
                            json.dumps(value['avoid_genres']), generation)).rowcount
    if not changed:
        raise ValueError('Unknown clicked pool generation')


def read_parameters(conn, surface):
    row = conn.execute('SELECT p.clicked_lane,p.clicked_epoch,p.clicked_include_genres,p.clicked_avoid_genres '
        'FROM recommendation_generation_surfaces h JOIN recommendation_generations p ON p.id=h.generation_id '
        'WHERE h.selected=1 AND h.surface=? AND p.clicked_lane IS NOT NULL', (surface,)).fetchone()
    if row is None:
        return None
    return {'lane': row[0], 'epoch': row[1], 'include_genres': json.loads(row[2] or '[]'),
            'avoid_genres': json.loads(row[3] or '[]')}


