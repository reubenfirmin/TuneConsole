"""Refresh saved rankings when preferences decay or temporary feedback expires."""
import hashlib
import json

from yt_playlist.rec import choice_learning, home_vibe, musical_character, rec_params

DECAY_TICK_S = 3600


def ranking_configuration(store):
    """Saved inputs shared by every prepared ranker and Taste settings operation.

    Keep the complete result-shaping registry here so a newly exposed control
    cannot silently leave an older pool current. These dependencies request
    ranking work; model fitting has its own narrower configuration contract.
    """
    return {'records': store.rec_model.weight_records(),
            'vibe_version': home_vibe.VERSION,
            'musical_profile_version': musical_character.VERSION,
            'params': {spec.name: rec_params.get_param(store, spec.name)
                       for spec in rec_params.PARAMS}}


def preferences(store, now):
    ranking = ranking_configuration(store)
    records = ranking['records']
    # Decay has no source-row mutation. Refresh at most hourly while there is
    # something to decay; permanent zero mutes do not fade back toward one.
    decaying = any(row['weight'] != 1 and row['updated_at'] is not None
                   and not (axis.startswith('genre:') and row['weight'] == 0)
                   for axis, row in records.items())
    choices = store.rec_proposals.get_proposals('choice_interests') or {}
    bandit = store.rec_proposals.get_proposals('choice_bandit') or {}
    contexts = choice_learning.contexts(store, now)
    choice_decay = any(sum(row['layer_weights'][layer] for layer in ('now', 'session', 'recent')) >= .001
                       for row in contexts)
    configuration = {**ranking,
                     'choices': choices.get('interest_version'),
                     'bandit': bandit.get('state_version'),
                     'bandit_active': bandit.get('expires_at') is None or now < bandit['expires_at'],
                     'album_feedback': store.settings.get_setting('album_feedback'),
                     'decay_tick': int(now // DECAY_TICK_S) if decaying or choice_decay else None}
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
