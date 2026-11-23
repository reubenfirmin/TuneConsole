"""Eligibility and bounded selection for On Air's initial queue."""
from contextlib import nullcontext
import hashlib

from yt_playlist.rec import radio_rotation, radio_tuning, rec_params


def allowed_seed(track, blocked, muted, maximum, genre_allowed):
    return (track.get('video_id') and track.get('key') and track['key'] not in blocked
            and track.get('available') != 0
            and (track.get('artist') or '').casefold() not in muted
            and genre_allowed(track.get('genre'))
            and (not track.get('duration') or track['duration'] <= maximum))


def eligible_seeds(store, tracks, now, *, current=None):
    """Recheck a saved offer in place, preserving its order and attribution."""
    if not tracks:
        return []
    with (nullcontext(store) if getattr(store, '_read_only', False) else store.read_view()) as reader:
        if current is None:
            current = reader.rec_eligibility.offered_tracks(track.get('video_id') for track in tracks)
        rotation = reader.radio_catalog.rotation_facts([track['key'] for track in current.values()])
        blocked = reader.rec_model.suppressed_keys('for_you', now, keys=[track['key'] for track in current.values()])
        muted = {artist.casefold() for artist in reader.rec_model.muted_artists()}
        weights = reader.rec_model.get_weights(now=now)
        maximum = 60 * rec_params.get_param(reader, 'max_track_minutes')
        axes = reader.genres.feature_axes(track.get('genre') for track in current.values())

        def genre_allowed(label):
            family, subgenre = axes.get(label, ('', None))
            return weights.get(f'genre:{family}', 1) != 0 and weights.get(f'genre:{subgenre}', 1) != 0

        result = []
        for offered in tracks:
            track = current.get(offered.get('video_id'))
            if (track and track['key'] == offered.get('key')
                    and allowed_seed(track, blocked, muted, maximum, genre_allowed)):
                result.append({**offered, **track, **radio_rotation.metadata(rotation.get(track['key']))})
        return result


def startup_shortlist(prepared, reader, group, count, history, *, allowed, axes, sample):
    pool = prepared.tracks('radio', group, 'membership', allowed=allowed, reader=reader,
        used={track['key'] for track in history}, limit=24, include_display=False,
        sample_membership=sample, prepare_page=axes)
    return _select_startup(pool, reader, group, count, history, allowed, prepared.comfort,
                           lambda keys: prepared.track_facts('rotation', keys, reader.radio_catalog.rotation_facts))


def _select_startup(pool, reader, group, count, history, allowed, comfort, rotation_facts):
    preferred = reader.tracks.preferred_playback_uploads(track['video_id'] for track in pool)
    pool = [{**track, **preferred.get(track['video_id'], {}), '_radio_mode': group} for track in pool]
    facts = rotation_facts({track['key'] for track in pool})
    for track in pool:
        track.update(radio_rotation.metadata(facts.get(track['key'])))
    return _pick_startup(pool, count, history, allowed, comfort)


def _pick_startup(pool, count, history, allowed, comfort):
    picks = []
    for _ in range(count):
        sequence = [*history, *picks]
        videos, keys = {t['video_id'] for t in sequence}, {t['key'] for t in sequence}
        available = [track for track in pool if track['video_id'] not in videos
                     and track['key'] not in keys and allowed(track)]
        if not available:
            break
        gap = max(radio_rotation.artist_gap(track, sequence) for track in available)
        tier = [track for track in available if radio_rotation.artist_gap(track, sequence) == gap]
        rotation = radio_rotation.source(tier, sequence, comfort)
        # Prefer a prepared length within the same rotation and spacing tier.
        # An unmeasured successor would require a provider call before saving.
        pick = dict(next((track for track in rotation if track.get('duration')), rotation[0]))
        radio_rotation.issue(pick)
        picks.append(pick)
    return picks


def prepared_launch_tracks(reader, history, count, now, maximum, *, selected=None):
    """Validate the selection and pick successors using one set of current facts."""
    seed = history[0]
    pools = reader.radio_catalog.launch_pools(seed['key'])
    if not pools:
        return []
    records = reader.rec_model.weight_records()
    muted = {artist.casefold() for artist in reader.rec_model.muted_artists()}
    weights = reader.rec_model.get_weights(now=now, records=records)
    genre_mutes = {axis for axis, value in weights.items() if axis.startswith('genre:') and value == 0}

    def pick(pool, sequence, remaining, selection):
        tracks = _launch_pool_tracks(reader, pool, seed['key'], now) if pool else []
        keys = {track['key'] for track in tracks}
        labels = {track.get('genre') for track in tracks} if genre_mutes else set()
        if selection is not None:
            keys.add(selection['key'])
            if genre_mutes:
                labels.add(selection.get('genre'))
        blocked = reader.rec_model.suppressed_keys('for_you', now, keys=keys, records=records)
        axes = reader.genres.feature_axes(labels) if labels else {}

        def genre_allowed(label):
            family, genre = axes.get(label, ('', None))
            return f'genre:{family}' not in genre_mutes and f'genre:{genre}' not in genre_mutes

        def allowed(track):
            return allowed_seed(track, blocked, muted, maximum, genre_allowed)

        if selection is not None and not allowed(selection):
            return None
        facts = reader.radio_catalog.rotation_facts(keys)
        if selection is not None:
            seed.update(radio_rotation.metadata(facts.get(selection['key'])))
            radio_rotation.issue(seed)
        for track in tracks:
            track.update(radio_rotation.metadata(facts.get(track['key'])))
        return _pick_startup(tracks, remaining, sequence, allowed, radio_tuning.DEFAULTS['comfort'])

    group, reserve = pools.get('radio'), pools.get('home')
    picks = pick(group or reserve, history, count, selected)
    if picks is None:
        return []
    if group and reserve and len(picks) < count:
        picks.extend(pick(reserve, [*history, *picks], count - len(picks), None))
    return picks


def _launch_pool_tracks(reader, pool, seed_key, now):
    size, limit = pool['eligible_count'], 48
    seed_bytes = repr((seed_key, int(now // 7200))).encode()
    start = int.from_bytes(hashlib.sha256(seed_bytes).digest()[:8]) % size if size > limit else 0
    wrap = max(0, start + limit - size) if start else 0
    tracks = reader.radio_catalog.starting_group_tracks(pool['id'], start, limit, wrap)
    for track in tracks:
        track.update(pool_policy=pool['policy_version'], _radio_catalog_score=track['score'] or 0,
                     serving_context={'recommendation_generation_id': pool['generation_id'],
                                      'recommendation_pool_id': pool['id'],
                                      'recommendation_pool_rank': track['pool_rank']})
        if pool['surface'] == 'radio':
            track['_radio_mode'] = pool['mode_key']
    return tracks
