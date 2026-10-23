"""Refresh saved rankings when preferences decay or temporary feedback expires."""
import hashlib
import json

from yt_playlist.rec import rec_params

DECAY_TICK_S = 3600


def preferences(store, now):
    records = {axis: row for axis, row in store.rec_model.weight_records().items()
               if axis.startswith('genre:')}
    half_life = rec_params.get_param(store, 'weight_revert_halflife_d')
    # Decay has no source-row mutation. Refresh at most hourly while there is
    # something to decay; permanent zero mutes do not fade back toward one.
    decaying = any(row['weight'] not in (0, 1) and row['updated_at'] is not None
                   for row in records.values())
    configuration = {'records': records, 'half_life': half_life,
                     'decay_tick': int(now // DECAY_TICK_S) if decaying else None}
    fingerprint = hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()
    return fingerprint


def due(store, now):
    deadline = store.settings.get_setting('rec_candidates_expire_at')
    return ((bool(deadline) and float(deadline) <= now)
            or store.settings.get_setting('rec_candidate_preferences') != preferences(store, now))


def prepared_settings(reader, now):
    """Capture alongside the rankings; failed or stale publication never marks them current."""
    deadline = reader.rec_model.next_feedback_expiry(now)
    return {'rec_candidate_preferences': preferences(reader, now),
            'rec_candidates_expire_at': str(deadline) if deadline is not None else ''}
