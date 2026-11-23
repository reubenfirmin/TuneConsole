"""Capture coherent prepared input snapshots for On Air."""
from contextlib import nullcontext
from itertools import islice
from yt_playlist.rec import embedding_vectors as vectors, group_viz
from yt_playlist.rec.stored_radio import initial_pool, LOOKAHEAD


def prepared_inputs(store, *, prepared, blocked=(), muted=(), comfort=.5, capture=None):
    """Read one committed model/pool generation; inflate after closing its reader."""
    owned = (getattr(store, '_read_only', False)
             or getattr(store._lock, 'held_by_current_thread', False))
    with (nullcontext(store) if owned else store.read_view(seconds=None)) as reader:
        if capture is not None:
            blocked, muted = capture(reader)
        generation = reader.rec_model.recording_state().generation
        encoding_model = reader.rec_model.recording_encoder()
        space = vectors.content_space_id(reader, snapshot={'model': encoding_model})
        head = group_viz.prepared(reader)
        modes = {group['version']: {**group, 'label': group['name']}
                 for group in (head or {}).get('groups', ()) if group.get('leaf')}
        captured = prepared.capture_inputs(reader, modes, space, generation, blocked, muted, comfort,
                                           adaptive=bool(head))
    bundles = prepared.decode_inputs(captured)
    return generation, encoding_model, space, modes, {**bundles, '_group_head': head}


def initial_projection(bundles, modes, space, generation, blocked, muted, comfort):
    """Restore evidence only after choosing the existing bounded live lookahead."""
    result = {key: bundles[key] for key in ('_radio', '_ppr', '_meta') if key in bundles}
    catalog = bundles.get('_radio') or {}
    members = (catalog.get('members') or {}) if catalog.get('space') == space else {}
    has_pool = False
    for mid in modes:
        pool = initial_pool(bundles.get(mid, {}), mid, blocked, muted, comfort)
        result[mid] = {'_radio_pool': pool}
        has_pool = has_pool or bool(pool or members.get(mid) and (
            modes[mid].get('version') == mid or catalog.get('generation') == generation))
    if 'all' in bundles:
        # Owned group membership and the discovery reserve have separate owners.
        shared = bundles['all']
        if has_pool:
            eligible = (track for track in shared.get('fresh', ()) if track.get('video_id')
                        and track['key'] not in blocked and (track.get('artist') or '').casefold() not in muted)
            result['all'] = {'fresh': list(islice(eligible, LOOKAHEAD))}
        else:
            result['all'] = shared
    return result
