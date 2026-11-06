"""Ranking freshness consumes choice decay without restoring unrelated learned evidence."""
import hashlib
import json
import sqlite3

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import mix_choices
from yt_playlist.rec import album_feedback_state, candidate_refresh, choice_learning, rec_params


def full_fingerprint(store, now):
    ranking = candidate_refresh.ranking_configuration(store)
    decaying = any(row['weight'] != 1 and row['updated_at'] is not None
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
                     'decay_tick': int(now // 3600) if decaying or transient else None}
    return hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()


def test_refresh_skips_vectors_tracks_estimates_and_album_history(store):
    context = {'format': 'albums', 'option_id': 'release', 'at': 100, 'days': 3,
               'episode_support': .5, 'feature': [.25] * 600, 'keys': ['unknown|artist']}
    store.rec_proposals.put_proposals('choice_interests', {
        'interest_version': 'current', 'contexts': [context] * 12,
        'album_track_days': {'release': {'unknown|artist': 3}}}, 100)
    store.rec_proposals.put_proposals('choice_bandit', {
        'state_version': 'current-bandit', 'expires_at': 3600,
        'rows': [{'mode_id': 999, 'estimate': .5}]}, 100)
    expected = full_fingerprint(store, 1000)
    denied = {'choice_interest_features', 'choice_interest_tracks', 'choice_mode_estimates',
              'choice_group_estimates', 'choice_album_interests', 'choice_album_track_days'}
    with store.read_view() as reader:
        def authorize(action, table, *rest):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table in denied else sqlite3.SQLITE_OK
        reader.conn.set_authorizer(authorize)
        try:
            assert candidate_refresh.preferences(reader, 1000) == expected
        finally:
            reader.conn.set_authorizer(None)


def test_refresh_preserves_transient_boundaries_future_and_mix_precision(store):
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
    with store.read_view(tracked=True) as reader:
        original = candidate_refresh.preferences(reader, now)
    store.rec_proposals.put_proposals('choice_interests', {'interest_version': 'new'}, now)
    with store.read_view(tracked=True) as reader:
        changed = candidate_refresh.preferences(reader, now)
        assert changed != original
        assert changed == full_fingerprint(reader, now)
    store.rec_proposals.put_proposals('choice_bandit', {'state_version': 'new'}, now)
    with store.read_view(tracked=True) as reader:
        assert candidate_refresh.preferences(reader, now) != changed
        assert candidate_refresh.preferences(reader, now) == full_fingerprint(reader, now)


@pytest.mark.parametrize('installed', [92], indirect=True)
def test_refresh_uses_legacy_and_selected_preparation_owners(installed):
    from tests.core.test_choice_learning_preparation import finish
    from tests.core.test_choice_publication import publish
    from yt_playlist.core.preparation import choice_learning as preparation, choice_publication

    store, _ = installed
    values = ({'interest_version': 'old', 'contexts': [
                  {'format': 'albums', 'at': 100, 'days': 2, 'episode_support': .5,
                   'feature': [.25], 'keys': ['unknown|artist']}]},
              {'state_version': 'old-bandit', 'expires_at': 3600})
    for surface, value in zip(('choice_interests', 'choice_bandit'), values):
        store.rec_proposals.put_proposals(surface, value, 100)
    assert store.rec_proposals.choice_refresh_inputs() == values
    preparation.JOB.install(store.conn)
    finish(store.conn)
    publish(store.conn)
    assert choice_publication.selected(store.conn) is not None
    assert not store._choice_learning
    # Selected relations must serve the fallback even when original payloads are denied.
    def authorize(action, table, column, *rest):
        if action == sqlite3.SQLITE_READ and (table == 'rec_proposals' and column == 'payload'
                or table == 'prepared_choice_learning_original_parts'):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    with store.read_view() as reader:
        reader.conn.set_authorizer(authorize)
        try:
            assert reader.rec_proposals.choice_refresh_inputs() == values
        finally:
            reader.conn.set_authorizer(None)
