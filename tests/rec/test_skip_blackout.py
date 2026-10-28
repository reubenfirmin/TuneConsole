"""A skip is a temporary recording break, including on cached Radio plans."""
from types import SimpleNamespace

import pytest

from tests.test_radio_catalog import catalog_track
from yt_playlist.core.store import Store
from yt_playlist.library import player_events
from yt_playlist.rec import eligibility, radio_tuning
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.rec.radio_directions import RadioDirections
from yt_playlist.rec.radio_policy import CandidateView, FeedbackContext, PlaybackHistory, RadioPolicy

DAY = 86400


@pytest.fixture
def listener(store):
    return store.identities.upsert_identity('Main', 'credential', None, True)


def exit_event(store, listener, track, at, *, kind='track_exit', position=20, duration=200):
    return store.player_events.record_player_event(
        listener, kind, track['video_id'], position, duration, 'PL', None, at)


def test_first_skip_blocks_for_24_hours_without_clearing_a_heart(store, listener):
    track, _ = catalog_track(store, 'skipped')
    store.rec_model.record_like(track['key'], 99)
    feedback = [tuple(row) for row in store.conn.execute('SELECT * FROM rec_feedback')]
    exit_event(store, listener, track, 100)
    assert store.player_events.skip_blackouts(100) == {track['key']: 100 + DAY}
    assert track['key'] in store.rec_model.suppressed_keys('for_you', 100 + DAY - 1)
    assert track['key'] in eligibility.home_exclusions(store, [track['key']], 101)
    assert not store.rec_model.suppressed_keys('for_you', 100 + DAY)
    assert track['key'] in store.rec_model.recent_liked_keys()
    assert [tuple(row) for row in store.conn.execute('SELECT * FROM rec_feedback')] == feedback


def test_repeated_skips_extend_the_break_but_old_repetitions_are_forgotten(store, listener):
    track, _ = catalog_track(store, 'repeat')
    for index, days in enumerate((1, 2, 4, 7, 7)):
        at = (index * 2 + 1) * DAY
        exit_event(store, listener, track, at)
        assert store.player_events.skip_blackouts(at) == {track['key']: at + days * DAY}
    later = 50 * DAY
    exit_event(store, listener, track, later)
    assert store.player_events.skip_blackouts(later) == {track['key']: later + DAY}


@pytest.mark.parametrize('keys_filtered', [False, True])
@pytest.mark.parametrize('kind', ['next', 'track_exit'])
def test_repeat_break_keeps_its_expiry_after_supporting_history_ages_out(
        store, listener, keys_filtered, kind):
    track, _ = catalog_track(store, 'aging-repeat')
    exit_event(store, listener, track, DAY, kind=kind)
    exit_event(store, listener, track, 30 * DAY, kind=kind)
    keys = [track['key']] if keys_filtered else None
    for now in (30 * DAY, 31.5 * DAY, 32 * DAY - 1):
        assert store.player_events.skip_blackouts(now, keys=keys) == {track['key']: 32 * DAY}
    assert not store.player_events.skip_blackouts(32 * DAY, keys=keys)
    # Old repetitions expire for a new skip, even though retrieval retains them.
    exit_event(store, listener, track, 61 * DAY, kind=kind)
    assert store.player_events.skip_blackouts(61 * DAY, keys=keys) == {track['key']: 62 * DAY}


def test_new_shorter_break_cannot_shorten_an_active_seven_day_break(store, listener):
    track, _ = catalog_track(store, 'aging-cluster')
    for at in (DAY, DAY + 100, DAY + 200, 31 * DAY):
        exit_event(store, listener, track, at)
    # The oldest supporting skip is exactly thirty days before the last one.
    assert store.player_events.skip_blackouts(31 * DAY) == {track['key']: 38 * DAY}
    # All three early skips age out. A new skip earns two days, while the
    # previously established seven-day break must remain until day 38.
    exit_event(store, listener, track, 32 * DAY)
    for now in (32 * DAY, 37 * DAY, 38 * DAY - 1):
        assert store.player_events.skip_blackouts(now) == {track['key']: 38 * DAY}
    assert not store.player_events.skip_blackouts(38 * DAY)


def test_exit_goodbye_and_next_for_one_playback_do_not_escalate(store, listener):
    track, _ = catalog_track(store, 'duplicate')
    exit_event(store, listener, track, 100, kind='next', position=20)
    exit_event(store, listener, track, 110, position=30)
    exit_event(store, listener, track, 111, kind='bye', position=30)
    assert store.player_events.skip_blackouts(111) == {track['key']: 100 + DAY}
    # A second playback has a later estimated start, even at the same position.
    exit_event(store, listener, track, 200, position=20)
    assert store.player_events.skip_blackouts(200) == {track['key']: 200 + 2 * DAY}


@pytest.mark.parametrize('position', [20, None])
def test_a_delayed_exit_deduplicates_an_explicit_request_without_a_duration(store, listener, position):
    track, _ = catalog_track(store, 'missing-clock')
    exit_event(store, listener, track, 100, kind='next', position=position, duration=None)
    exit_event(store, listener, track, 110, position=30)
    assert store.player_events.skip_blackouts(110) == {track['key']: 100 + DAY}


@pytest.mark.parametrize('position,duration', [(0, 200), (180, 200), (None, None)])
def test_explicit_next_is_retained_without_a_skip_shaped_exit(store, listener, position, duration):
    track, _ = catalog_track(store, 'explicit')
    msg = {'kind': 'next', 'videoId': track['video_id'], 'brandKnown': True,
           'brandId': '', 'position': position, 'duration': duration, 'playlist': 'PL'}
    assert player_events.handle_player_event(SimpleNamespace(store=store), msg, 100)
    assert store.player_events.skip_blackouts(100) == {track['key']: 100 + DAY}
    assert store.player_events.recent_skips_with_ts(0) == []
    assert not store.rec_model.disliked_identity_keys()


@pytest.mark.parametrize('position,duration', [(0, 200), (190, 200), (100, 200),
                                               (None, None), (20, 0), (float('inf'), 200)])
def test_non_skip_observations_do_not_create_a_blackout(store, listener, position, duration):
    track, _ = catalog_track(store, 'not-a-skip')
    exit_event(store, listener, track, 100, position=position, duration=duration)
    assert not store.player_events.skip_blackouts(100)


def test_future_ticks_unknown_accounts_and_empty_key_filters_are_excluded(store, listener):
    track, _ = catalog_track(store, 'future')
    exit_event(store, listener, track, 101)
    exit_event(store, listener, track, 100, kind='tick')
    assert not store.player_events.skip_blackouts(100)
    assert not store.player_events.skip_blackouts(101, keys=[])
    assert not store.player_events.skip_blackouts(101, keys=['unrelated'])
    assert not player_events.handle_player_event(SimpleNamespace(store=store),
        {'kind': 'next', 'videoId': track['video_id'], 'brandId': ''}, 100)
    assert not player_events.handle_player_event(SimpleNamespace(store=store),
        {'kind': 'next', 'videoId': track['video_id'], 'brandId': 'unknown', 'brandKnown': True}, 100)


def test_uploads_share_a_recording_break_and_unknown_uploads_can_be_resolved_later(store, listener):
    track, tid = catalog_track(store, 'known')
    # The request is retained before discovery has a recording for this upload.
    alternate = {**track, 'video_id': 'later-upload'}
    exit_event(store, listener, alternate, 100)
    assert not store.player_events.skip_blackouts(100)
    store.tracks.upsert_track(alternate['video_id'], 'Another upload', 'Uploader', '', 200)
    rid = store.tracks.catalog_subject(tid)['recording_id']
    store.tracks.associate_youtube_item(alternate['video_id'], rid, source='fixture',
        evidence={'controlled': 'same recording'}, now=101, same_recording=True)
    assert store.player_events.skip_blackouts(102, keys=[track['key']]) == {track['key']: 100 + DAY}


def test_a_file_backed_restart_retains_the_break(tmp_path):
    path = tmp_path / 'library.db'
    store = Store(path)
    store.init_schema()
    iid = store.identities.upsert_identity('Main', 'credential', None, True)
    track, _ = catalog_track(store, 'restart')
    exit_event(store, iid, track, 100)
    store.conn.close()
    store = Store(path)
    try:
        store.init_schema()
        assert store.player_events.skip_blackouts(101) == {track['key']: 100 + DAY}
        assert not store.player_events.skip_blackouts(100 + DAY)
    finally:
        store.conn.close()


@pytest.mark.parametrize('energy', [0, .5, 1])
def test_live_skip_removes_a_cached_radio_choice_and_cannot_return_on_refill(store, listener, energy):
    tracks = [catalog_track(store, f'radio-{i}')[0] for i in range(32)]
    store.rec_proposals.put_proposals('mode_bundles', {'all': {'wheelhouse': tracks}})
    candidates = RadioCandidates(store, 100, {**radio_tuning.DEFAULTS, 'energy': energy})
    directions = RadioDirections(candidates, tracks[0])
    directions.update([tracks[0]], [], 0)
    rejected = directions.slots[directions.active]['tracks'][0]
    exit_event(store, listener, rejected, 110)
    view = CandidateView(candidates, directions, (tracks[0],), frozenset(), frozenset())
    history = PlaybackHistory((tracks[0],), (tracks[0],), 0, 0, 0)
    policy = RadioPolicy(store, lambda: 111)
    # The private current-layer sample is older than this new raw skip.
    assert candidates.now_sampled_at == 100
    update = policy.feedback(view, FeedbackContext('PL', listener, 100, {}, None, (), frozenset()))
    directions.blocked = set(update.blocked)
    assert update.changed
    assert RadioPolicy.invalid(view, history, rejected)
    selected = policy.pick(view)
    assert selected is not None and selected['key'] != rejected['key']
    candidates.refresh_exclusions(111)
    future = candidates.next_track([tracks[0]], target=rejected)
    assert future is not None and future['key'] != rejected['key']
    # Refills must not overwrite the newer feedback cutoff with prepared_at.
    assert rejected['key'] in candidates.blocked
    assert candidates.eligibility_at == 111
    assert rejected['key'] not in {track['key'] for track in candidates.seed_offer(32)}

