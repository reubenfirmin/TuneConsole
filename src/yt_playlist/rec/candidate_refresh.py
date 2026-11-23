"""Hourly admission for captured recommendation runs and their refresh inputs."""
import hashlib
import json

from yt_playlist.rec import album_feedback_state, artist_model, artist_variety, choice_learning, home_vibe, musical_character, rec_params
from yt_playlist.core.mix_choices import PRECISION
from yt_playlist.core.ranking_checkpoints import CHECKED_AT, CHECKED_GROUP
from yt_playlist.core.rec_model_policy import STARTUP_WEIGHT_POLICY, session_identity
from yt_playlist.library.listening_window import listening_window
from yt_playlist.library import mix_listening
from yt_playlist.rec.content_projection import VERSION as COLD_PROJECTION_VERSION
from yt_playlist.rec.layers import POLICY_VERSION
from yt_playlist.rec.radio_intensity import VERSION as RADIO_INTENSITY_VERSION

DECAY_TICK_S = 3600
CHECK_INTERVAL_S = 3600
_RANKING_PARAMETERS = {spec.name: rec_params.SETTING_PREFIX + spec.name for spec in rec_params.PARAMS
                       if spec.name not in rec_params.LEGACY_MODE_PARAMS}


def check_due(store, now):
    checked = store.settings.get_setting(CHECKED_AT)
    expires = store.settings.get_setting('rec_candidates_expire_at')
    return (not checked or now >= float(checked) + CHECK_INTERVAL_S or bool(expires and now >= float(expires))
            or (store.settings.get_setting(CHECKED_GROUP) or '') != (store.taste_groups.head_generation() or ''))


def checked(store, now):
    store.settings.publish_values({CHECKED_AT: str(now), CHECKED_GROUP: store.taste_groups.head_generation() or ''})


def ranking_configuration(store):
    """Saved inputs shared by every prepared ranker and Taste settings operation.

    Keep the complete result-shaping registry here so a newly exposed control
    cannot silently leave an older pool current. These dependencies request
    ranking work; model fitting has its own narrower configuration contract.
    """
    if (store._db_path is not None and not getattr(store, '_read_only', False)
            and not store._lock.held_by_current_thread):
        with store.read_view() as reader:
            return ranking_configuration(reader)
    params = store.rec_model.parameter_values(tuple(_RANKING_PARAMETERS.values()))
    return {'records': store.rec_model.weight_records(),
            'performing_artist_version': artist_variety.VERSION,
            'cold_artist_projection_version': artist_model.PROJECTION_POLICY,
            'cold_track_projection_version': COLD_PROJECTION_VERSION,
            'mix_listening_version': mix_listening.VERSION,
            'mix_choice_precision': PRECISION,
            'vibe_version': home_vibe.VERSION,
            'musical_profile_version': musical_character.VERSION,
            'params': {name: rec_params.parameter_value(name, params.get(key))
                       for name, key in _RANKING_PARAMETERS.items()}}


def preferences(store, now, *, include_exposure=True):
    """Exposure history refreshes rankings without invalidating Home's held menu."""
    if (store._db_path is not None and not getattr(store, '_read_only', False)
            and not store._lock.held_by_current_thread):
        with store.read_view() as reader:
            return preferences(reader, now, include_exposure=include_exposure)
    return store.memoized_read(('candidate-preferences', now, home_vibe.VERSION,
                                musical_character.VERSION, artist_variety.VERSION, PRECISION, include_exposure,
                                RADIO_INTENSITY_VERSION if include_exposure else None),
                               lambda: _preferences(store, now, include_exposure))


def _preferences(store, now, include_exposure):
    ranking = ranking_configuration(store)
    ranking['listener_layers'] = listener_inputs(store, now)
    choices, bandit = store.rec_proposals.choice_refresh_inputs()
    recording = store.rec_model.recording_state()
    models = {'content': recording.generation, 'collaborative': recording.fit_fingerprint}
    if include_exposure:
        # Refresh prepared ranks while preserving the tuning identity of a held Mix Tapes menu.
        ranking['radio_intensity_version'] = RADIO_INTENSITY_VERSION
        if ranking['listener_layers']['permanent'] is None:
            ranking['startup_weight_policy'] = STARTUP_WEIGHT_POLICY
        ranking['recording_exclusions'] = sorted(store.rec_model.disliked_identity_keys(now=now))
        models['groups'] = store.taste_groups.head_generation()
        ranking['mix_listening'] = mix_listening.rank_input(store, now)
    return preference_fingerprint(ranking, choices=choices, bandit=bandit,
        choice_decay=choice_learning.transient_active(store, choices, now),
        album_feedback=album_feedback_state.state(store),
        models=models,
        home={'genre_request': store.rec_proposals.get_proposals('home_genre_request'),
              'vibe': store.settings.get_setting('home_mix_vibe'),
              'tuning_at': store.settings.get_setting('home_mix_tuning_updated_at')},
        now=now, exposure=({'card_views': store.rec_ledger.card_views('cards')} if include_exposure else {}))


def listener_inputs(store, now):
    window = listening_window(store, now)
    return {'version': POLICY_VERSION, 'now': session_identity(window.active),
            'today': sorted(window.days.values()), 'permanent': store.rec_model.permanent(),
            'steering': store.rec_model.get_session_weights(now)}


def preference_fingerprint(ranking, *, choices, bandit, choice_decay, album_feedback, models,
                           home, now, exposure):
    """Freshness identity from captured inputs; wall-clock decay never needs a source write."""
    records = ranking['records']
    # Decay has no source-row mutation. Refresh at most hourly while there is
    # something to decay; permanent zero mutes do not fade back toward one.
    decaying = ranking.get('listener_layers', {}).get('permanent') is None and any(
                   row['weight'] != 1 and row['updated_at'] is not None
                   and not (axis.startswith('genre:') and row['weight'] == 0)
                   for axis, row in records.items())
    configuration = {**ranking,
                     'choices': choices.get('interest_version'),
                     'bandit': bandit.get('state_version'),
                     'bandit_active': bandit.get('expires_at') is None or now < bandit['expires_at'],
                     'album_feedback': album_feedback,
                     'models': models, 'home': home,
                     'decay_tick': int(now // DECAY_TICK_S) if decaying or choice_decay else None}
    configuration.update(exposure)
    fingerprint = hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()
    return fingerprint


def due(store, now):
    deadline = store.settings.get_setting('rec_candidates_expire_at')
    if deadline and float(deadline) <= now:
        return True
    return refresh_due(deadline, store.settings.get_setting('rec_candidate_preferences'), preferences(store, now), now)


def refresh_due(deadline, previous, current, now):
    return (bool(deadline) and float(deadline) <= now) or previous != current


def prepared_settings(reader, now):
    """Capture alongside the rankings, leaving later changes due for the next round."""
    deadline = reader.rec_model.next_feedback_expiry(now)
    day_or_session_end = listening_window(reader, now).expires_at
    deadline = min(deadline, day_or_session_end) if deadline is not None else day_or_session_end
    return {'rec_candidate_preferences': preferences(reader, now),
            'rec_candidates_expire_at': str(deadline) if deadline is not None else ''}
