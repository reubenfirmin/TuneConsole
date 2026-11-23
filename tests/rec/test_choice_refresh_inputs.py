"""Ranking freshness consumes choice decay without restoring unrelated learned evidence."""
import hashlib
import json

import pytest

from tests.core.test_predecessor_writes import installed
from tests.store_fixture_support import (
    bounded_consumer_schema, file_consumer_store as store, native_sql_calls,
)
from yt_playlist.core import choice_learning_state, mix_choices
from yt_playlist.rec import album_feedback_state, candidate_refresh, choice_learning, rec_params
from yt_playlist.rec.radio_intensity import VERSION as RADIO_INTENSITY_VERSION


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Fingerprint and SQL ownership checks construct no HTTP application."""


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('jazz', 'Jazz'), ('house', 'House'))) as schema:
        yield schema


def test_refresh_input_keeps_empty_typed_owners_and_real_guards(_pristine_current_schema):
    assert _pristine_current_schema.execute('PRAGMA query_only').fetchone()[0] == 1
    assert _pristine_current_schema._revision_script
    assert not _pristine_current_schema.execute("SELECT 1 FROM settings WHERE key GLOB 'choice_learned:*'").fetchone()
    assert not _pristine_current_schema.execute('PRAGMA foreign_key_check').fetchone()


def full_fingerprint(store, now):
    from yt_playlist.library import mix_listening

    ranking = candidate_refresh.ranking_configuration(store)
    ranking['listener_layers'] = candidate_refresh.listener_inputs(store, now)
    ranking['radio_intensity_version'] = RADIO_INTENSITY_VERSION
    if ranking['listener_layers']['permanent'] is None:
        ranking['startup_weight_policy'] = 'exclude-legacy-dislikes'
    ranking['recording_exclusions'] = sorted({row['item_key'] for row in store.rec_model.list_dislikes()
                                              if row['until'] is None or row['until'] > now})
    ranking['mix_listening'] = mix_listening.rank_input(store, now)
    decaying = ranking['listener_layers']['permanent'] is None and any(
        row['weight'] != 1 and row['updated_at'] is not None
        and not (axis.startswith('genre:') and row['weight'] == 0)
        for axis, row in ranking['records'].items())
    choices = store.rec_proposals.get_proposals('choice_interests') or {}
    bandit = store.rec_proposals.get_proposals('choice_bandit') or {}
    transient = any(sum(row['layer_weights'][layer] for layer in ('now', 'session', 'recent')) >= .001
                    for row in choice_learning.contexts(store, now))
    configuration = {**ranking, 'choices': choices.get('interest_version'),
                     'bandit': bandit.get('state_version'),
                     'bandit_active': bandit.get('expires_at') is None or now < bandit['expires_at'],
                     'album_feedback': album_feedback_state.state(store),
                     'models': {'content': store.rec_model.recording_state().generation,
                                'collaborative': store.rec_model.recording_state().fit_fingerprint,
                                'groups': store.taste_groups.head_generation()},
                     'card_views': store.rec_ledger.card_views('cards'),
                     'home': {'genre_request': store.rec_proposals.get_proposals('home_genre_request'),
                              'vibe': store.settings.get_setting('home_mix_vibe'),
                              'tuning_at': store.settings.get_setting('home_mix_tuning_updated_at')},
                     'decay_tick': int(now // 3600) if decaying or transient else None}
    return hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()


def test_refresh_skips_vectors_tracks_estimates_and_album_history(store, monkeypatch):
    context = {'format': 'albums', 'option_id': 'release', 'at': 100, 'days': 3,
               'episode_support': .5, 'feature': [.25] * 600, 'keys': ['unknown|artist']}
    store.rec_proposals.put_proposals('choice_interests', {
        'interest_version': 'current', 'contexts': [context] * 12,
        'album_track_days': {'release': {'unknown|artist': 3}}}, 100)
    store.rec_proposals.put_proposals('choice_bandit', {
        'state_version': 'current-bandit', 'expires_at': 3600,
        'rows': [{'mode_id': 999, 'estimate': .5}]}, 100)
    expected = full_fingerprint(store, 1000)
    def reject_full_publication(*args, **kwargs):
        pytest.fail('Ranking freshness loaded a full choice publication')

    monkeypatch.setattr(choice_learning_state, 'read', reject_full_publication)
    with store.read_view() as reader:
        interests, bandit = reader.rec_proposals.choice_refresh_inputs()
        assert interests == {'interest_version': 'current', 'contexts': [
            {'format': 'albums', 'at': 100, 'days': 3, 'episode_support': .5}
        ] * 12}
        assert bandit == {'state_version': 'current-bandit', 'expires_at': 3600}
        assert candidate_refresh.preferences(reader, 1000) == expected


def test_refresh_preserves_transient_boundaries_future_and_mix_precision(store, native_sql_calls):
    for name, value in {'now_gain': 1, 'now_window_h': 1, 'session_alpha': 0,
                        'choice_recent_w': 0, 'choice_saved_w': 1, 'choice_saved_days': 3}.items():
        rec_params.set_param(store, name, value)
    with store.conn:
        descriptor = mix_choices.descriptor(store.conn, {'label': 'Explore', 'genres': ['Jazz']})
    base = {'format': 'albums', 'at': 0, 'days': 3, 'episode_support': 1}
    cases = [
        (base, 3599, True), (base, 3600, False),  # Saved support does not decay.
        ({**base, 'at': 4000}, 3999, False),
        ({**base, 'episode_support': .001}, 0, True),
        ({**base, 'episode_support': .000999}, 0, False),
        ({**base, 'format': 'mixes'}, 0, False),
        ({**base, 'format': 'mixes', 'descriptor_id': descriptor}, 0, True),
    ]
    for index, (context, now, transient) in enumerate(cases):
        store.rec_proposals.put_proposals('choice_interests', {
            'interest_version': str(index), 'contexts': [context]}, 0)
        narrow, _ = store.rec_proposals.choice_refresh_inputs()
        assert choice_learning.transient_active(store, narrow, now) is transient
        assert candidate_refresh.preferences(store, now) == full_fingerprint(store, now)
    # Session stops at a day; recent support may continue after that boundary.
    rec_params.set_param(store, 'now_gain', 0)
    rec_params.set_param(store, 'session_alpha', 1)
    rec_params.set_param(store, 'session_halflife_h', 24)
    store.rec_proposals.put_proposals('choice_interests', {'contexts': [base]}, 0)
    narrow, _ = store.rec_proposals.choice_refresh_inputs()
    assert choice_learning.transient_active(store, narrow, 86399)
    assert not choice_learning.transient_active(store, narrow, 86400)
    rec_params.set_param(store, 'choice_recent_w', 1)
    assert choice_learning.transient_active(store, narrow, 86400)


def test_refresh_keeps_missing_and_null_headers_and_bandit_expiry(store):
    assert store.rec_proposals.choice_refresh_inputs() == ({}, {})
    for interests in ({}, {'interest_version': None}, {'interest_version': 'new', 'contexts': []}):
        store.rec_proposals.put_proposals('choice_interests', interests, None)
        assert store.rec_proposals.choice_refresh_inputs()[0] == interests
    for bandit in ({}, {'state_version': None, 'expires_at': None},
                   {'state_version': 'bandit', 'expires_at': 3600}):
        store.rec_proposals.put_proposals('choice_bandit', bandit, 0)
        assert store.rec_proposals.choice_refresh_inputs()[1] == bandit
        for now in (3599, 3600, 3601):
            assert candidate_refresh.preferences(store, now) == full_fingerprint(store, now)


def test_refresh_does_not_hide_invalid_later_context_behind_active_support(store):
    store.rec_proposals.put_proposals('choice_interests', {'contexts': [
        {'format': 'albums', 'at': 0, 'days': 1, 'episode_support': 1}, {'format': 'albums'}]}, 0)
    with pytest.raises(KeyError, match='at'):
        choice_learning.contexts(store, 0)
    with pytest.raises(KeyError, match='at'):
        candidate_refresh.preferences(store, 0)


def test_tracked_refresh_rechecks_replacement_choice_publications(file_backed_store):
    store, now = file_backed_store, 1000
    store.modes.replace_modes([{'mode_id': 101, 'label': 'House', 'families': [['house', 1]],
                               'centroid': [1.0], 'size': 1, 'rep_keys': []}], [], now)
    with store.read_view(tracked=True) as reader:
        original = candidate_refresh.preferences(reader, now)
    store.rec_proposals.put_proposals('choice_interests', {'interest_version': 'new'}, now)
    with store.read_view(tracked=True) as reader:
        changed = candidate_refresh.preferences(reader, now)
        assert changed != original
        assert changed == full_fingerprint(reader, now)
    store.rec_proposals.put_proposals('choice_bandit', {'state_version': 'new'}, now)
    with store.read_view(tracked=True) as reader:
        bandit = candidate_refresh.preferences(reader, now)
        assert bandit != changed
        assert candidate_refresh.preferences(reader, now) == full_fingerprint(reader, now)
    store.modes.log_impressions(5, [('wheelhouse', 101)], now)
    assert candidate_refresh.preferences(store, now) == bandit
    store.rec_ledger.bump_card_view('cards', now)
    assert candidate_refresh.preferences(store, now) != bandit
