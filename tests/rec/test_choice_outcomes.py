from types import SimpleNamespace

import pytest

from yt_playlist.library import player_events
from yt_playlist.rec import choice_evidence as choices, choice_outcomes as outcomes


def menu(store, oid='menu', now=100, size=4):
    return choices.issue(store, 'albums', [{'id': 'record', 'tracks': [
        {'video_id': f'video-{i}'} for i in range(size)]}], now, context={}, opportunity_id=oid)


def test_player_ingest_attributes_only_confirmed_exact_account_queue_and_track(store):
    iid = store.identities.upsert_identity('main', 'test', None, True)
    oid = menu(store)
    outcomes.bind(store, iid, 'OLAK-record', 'video-0', oid, 'record', 101)
    assert not outcomes.observe(store, iid + 1, 'OLAK-record', 'video-0', 'track_exit', 180, 200, 1, 102)
    assert not outcomes.observe(store, iid, 'other', 'video-0', 'track_exit', 180, 200, 2, 102)
    assert not outcomes.observe(store, iid, 'OLAK-record', 'other', 'track_exit', 180, 200, 3, 102)
    assert player_events.handle_player_event(SimpleNamespace(store=store), {
        'kind': 'track_exit', 'playlist': 'OLAK-record', 'videoId': 'video-0',
        'position': 180, 'duration': 200}, 103)
    event = store.rec_ledger.evidence().records('response')[0]
    assert event['details']['kind'] == 'track_exit'
    assert not outcomes.observe(store, iid, 'OLAK-record', 'video-0', 'track_exit', 180, 200,
                                event['details']['player_event_id'], 104)
    assert not outcomes.observe(store, iid, 'OLAK-record', 'video-0', 'track_exit', 180, 200, 4, 22000)


@pytest.mark.parametrize('size', [6, 24])
def test_episode_counts_and_track_coverage_are_normalized_and_missing_endings_stay_unknown(store, size):
    oid = menu(store, size=size)
    choices.respond(store, oid, 'choice', 101, option_id='record')
    choices.respond(store, oid, 'start', 102, option_id='record', details={'video_id': 'video-0'})
    for index in range(size // 2):
        choices.respond(store, oid, 'experience', 103 + index, option_id='record',
            action_id=f'track-{index}', details={'video_id': f'video-{index}'})
    row = choices.compile_report(store, 200)['formats']['albums']
    assert row['mean_track_coverage'] == .5
    assert row['observed_exit_episodes'] == row['completion_episodes'] == 0
    assert row['unknown_ending_episodes'] == 1
    for index in range(size // 2):
        choices.respond(store, oid, 'experience', 210 + index, option_id='record',
            action_id=f'exit-{index}', details={'video_id': f'video-{index}', 'kind': 'track_exit',
                                               'position': 190, 'duration': 200})
    row = choices.compile_report(store, 300)['formats']['albums']
    assert row['observed_exit_episodes'] == row['completion_episodes'] == 1
    assert row['unknown_ending_episodes'] == 0


def test_fresh_choice_on_another_day_is_a_return_but_continuation_and_tab_close_are_not(store):
    first = menu(store)
    choices.respond(store, first, 'choice', 101, option_id='record')
    choices.respond(store, first, 'start', 102, option_id='record')
    choices.respond(store, first, 'start', 86500, option_id='record', action_id='autoplay-next-day')
    assert choices.compile_report(store, 86501)['formats']['albums']['returns'] == 0
    second = menu(store, oid='revisit', now=86600)
    choices.respond(store, second, 'choice', 86601, option_id='record')
    choices.respond(store, second, 'start', 86602, option_id='record')
    choices.respond(store, second, 'experience', 86603, option_id='record', details={
        'video_id': 'video-0', 'kind': 'bye', 'position': 20, 'duration': 200})
    row = choices.compile_report(store, 86700)['formats']['albums']
    assert row['returns'] == 1
    assert row['early_exit_episodes'] == 0
    choices.respond(store, second, 'withdraw', 86701, details={'action_id': 'revisit:choice:record'})
    assert choices.compile_report(store, 86702)['formats']['albums']['returns'] == 0


def test_automatic_continuation_extends_experience_without_a_new_choice_or_start(store):
    iid = store.identities.upsert_identity('main', 'test', None, True)
    oid = choices.issue(store, 'radio', [{'id': 'direction', 'tracks': []}], 100,
                        context={}, opportunity_id='direction-menu', episode_id='session')
    choices.respond(store, oid, 'choice', 101, option_id='direction')
    choices.respond(store, oid, 'start', 102, option_id='direction')
    outcomes.bind(store, iid, 'radio-queue', 'one', oid, 'direction', 102)
    outcomes.continue_playback(store, iid, 'radio-queue', 'two', 200)
    assert outcomes.observe(store, iid, 'radio-queue', 'two', 'track_exit', 180, 200, 5, 400)
    row = choices.compile_report(store, 401)['formats']['radio']
    assert row['choices'] == row['starts'] == row['independent_episodes'] == 1
    assert row['completion_episodes'] == 1 and row['returns'] == 0
