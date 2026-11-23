import pytest

from yt_playlist.core import playback_feedback
from yt_playlist.core.rec_model_policy import PERMANENT_SURFACE, PERMANENT_WORK
from yt_playlist.rec import permanent_taste, transient

DAY = 86400


def _track(store, video, title, artist, genre):
    track = store.tracks.upsert_track(video, title, artist)
    store.tracks.set_track_genre(track, genre)
    return store.tracks.identity_key_for_video(video)


def _complete(store, until, page_size=2):
    for _ in range(30):
        if not permanent_taste.prepare(store, until, page_size=page_size):
            return store.rec_proposals.get_proposals(PERMANENT_SURFACE)
    raise AssertionError('Listener replay did not finish')


def test_restartable_baseline_uses_real_dates_and_independent_support(file_backed_store):
    store = file_backed_store
    account = store.accounts.upsert_account('listener', None, True)
    rock = _track(store, 'rock', 'Rock song', 'Rock artist', 'rock')
    techno = _track(store, 'techno', 'Techno song', 'Techno artist', 'techno')
    start = 20 * DAY
    now = start + 22 * DAY
    store.rec_model.set_weight('artist:Legacy', 2., now=start)
    store.rec_model.set_weight('genre:blocked', 0., lo=0., now=start)

    for index in range(3):
        store.history.record_play_event(account, rock, 'rock', start + 3600 + 300 * index, source_timezone='UTC')
    store.tracks.record_item_ratings(account, {'rock': 'LIKE'}, source='api', now=start + 4300)
    for days in (1, 8, 15):
        at = start + days * DAY + 3600
        assert store.history.record_play_event(account, techno, 'techno', at, source_timezone='UTC')
        assert not store.history.record_play_event(account, techno, 'techno', at, source_timezone='UTC')
    for days in (2, 9, 16):
        store.history.record_play_event(account, rock, 'rock', start + days * DAY + 3600, context_choice=True)
    store.tracks.record_item_ratings(account, {'techno': 'LIKE'}, source='api', now=start + 8 * DAY + 3610)

    assert permanent_taste.prepare(store, now, page_size=2)
    assert store.rec_proposals.get_proposals(PERMANENT_SURFACE) is None
    assert store.rec_model.baseline_weights(now)['artist:Legacy'] > 1.
    # A new play can arrive during replay. Old legacy weights still serve until the new generation is complete.
    store.history.record_play_event(account, techno, 'techno', start + 21 * DAY + 3600)
    result = _complete(store, now)
    assert result['facets']['artist:Techno artist']['days'] == 4
    assert result['facets']['artist:Rock artist']['days'] == 1
    assert 'artist:Rock artist' not in result['weights']
    assert result['weights']['artist:Techno artist'] > 1.
    expected = {**result['weights'], 'genre:blocked': 0.}
    assert store.rec_model.baseline_weights(now) == expected
    assert store.rec_model.baseline_weights(now + 300 * DAY) == expected
    assert _complete(store, now + 300 * DAY) == result
    assert store.rec_model.weight_records()['artist:Legacy']['weight'] == 2.


def test_recent_preserves_day_precision_and_removes_the_exact_play_shadow(store):
    account = store.accounts.upsert_account('listener', None, True)
    now = 20 * DAY + 300
    older = 'day evidence|artist'
    exact = 'exact evidence|artist'
    store.history.record_history_plays(account, now, [(older, 'yesterday'), (exact, 'today')], source_timezone='UTC')
    store.history.record_play_event(account, exact, None, now - 60, source_timezone='UTC')
    plays = dict(transient.recent_sources(store, now)[1])
    assert plays[exact] == now - 60
    assert isinstance(plays[older], transient.DatedPlay)
    assert plays[older].day == '1970-01-20'
    assert plays[older].age(now) == DAY
    assert plays[older].confidence < 1.


@pytest.mark.parametrize('page_size', [1, 2, 8])
@pytest.mark.parametrize('close_before_like', [False, True])
def test_midnight_like_strengthens_the_playback_episode_across_replay_pages(
        store, page_size, close_before_like):
    account = store.accounts.upsert_account('listener', None, True)
    track = _track(store, 'techno', 'Techno song', 'Techno artist', 'techno')
    midnight = 20 * DAY
    for at in (midnight - 300, midnight + 300, midnight + DAY + 300):
        store.history.record_play_event(account, track, 'techno', at, source_timezone='UTC')
    if close_before_like:
        store.player_events.record_player_event(account, 'bye', 'techno', 30, 300, None, '{}', midnight + 330)
    store.tracks.record_item_ratings(account, {'techno': 'LIKE'}, source='api', now=midnight + 360)

    result = _complete(store, midnight + 2 * DAY, page_size=page_size)
    for axis in ('genre:techno', 'artist:Techno artist'):
        facet = result['facets'][axis]
        assert facet['deliberate_days'] == 1
        assert facet['days'] == (3 if close_before_like else 2)
        assert facet['support'] == (1.5 if close_before_like else 1.25)
        assert (axis in result['weights']) == close_before_like


def test_replay_uses_audible_continuity_between_distant_plays(store):
    account = store.accounts.upsert_account('listener', None, True)
    track = _track(store, 'techno', 'Techno song', 'Techno artist', 'techno')
    midnight = 20 * DAY
    for at in (midnight - 4 * 3600, midnight + 300, midnight + DAY + 300):
        store.history.record_play_event(account, track, 'techno', at, source_timezone='UTC')
    for at in range(midnight - 4 * 3600 + 900, midnight + 300, 900):
        store.player_events.record_player_event(account, 'tick', 'techno', 30, 300, None, '{}', at)
    store.tracks.record_item_ratings(account, {'techno': 'LIKE'}, source='api', now=midnight + 360)

    result = _complete(store, midnight + 2 * DAY, page_size=1)
    assert result['facets']['artist:Techno artist']['days'] == 2
    assert result['facets']['artist:Techno artist']['support'] == 1.25
    assert 'artist:Techno artist' not in result['weights']


@pytest.mark.parametrize('published', [False, True])
def test_replay_revisits_independence_after_recovering_player_activity(file_backed_store, monkeypatch, published):
    store = file_backed_store
    account = store.accounts.upsert_account('listener', None, True)
    key = _track(store, 'techno', 'Techno song', 'Techno artist', 'techno')
    midnight = 20 * DAY
    now = midnight + 2 * DAY
    for at in (midnight - 1200, midnight + 1200, midnight + DAY + 1200):
        store.history.record_play_event(account, key, 'techno', at, source_timezone='UTC')
    store.tracks.record_item_ratings(account, {'techno': 'LIKE'}, source='api', now=midnight + 1260)
    if published:
        assert _complete(store, now)['facets']['artist:Techno artist']['days'] == 3
    else:
        for _ in range(2):
            assert permanent_taste.prepare(store, now, page_size=1)
        assert store.rec_proposals.get_proposals(PERMANENT_WORK)['after'][0] == midnight + 1200

    event = {'kind': 'tick', 'videoId': 'techno', 'position': 1200, 'duration': 2700,
             'paused': False, 'muted': False, 'volume': 100}
    playback_feedback.capture(store, 'player', account, event, midnight)
    # Keep the observation pending once after SQL projection.
    with monkeypatch.context() as patch:
        patch.setattr(store.playback_feedback, 'acknowledge_observation', lambda _capture: None)
        assert playback_feedback.drain(store, limit=1) == 1
    assert playback_feedback.drain(store) == 1
    assert len(store.player_events.playback_activity(midnight, midnight)) == 1

    for at in (now - 60, now - 30, now):
        # Forward ticks can arrive before each page without resetting it.
        store.player_events.record_player_event(account, 'tick', 'techno', 30, 300, None, '{}', at)
        assert permanent_taste.prepare(store, now, page_size=1)
    result = _complete(store, now, page_size=1)
    assert result['facets']['artist:Techno artist']['days'] == 2
    assert result['facets']['artist:Techno artist']['support'] == 1.25
    assert 'artist:Techno artist' not in result['weights']
    # A forward frame after the last replayed observation leaves this result current.
    store.player_events.record_player_event(account, 'tick', 'techno', 30, 300, None, '{}', now + 60)
    assert not permanent_taste.prepare(store, now + 60, page_size=1)
    assert store.rec_proposals.get_proposals(PERMANENT_SURFACE) == result

    for surface in (PERMANENT_SURFACE, PERMANENT_WORK):
        store.rec_proposals.put_proposals(surface, {}, now)
    chronological = _complete(store, now, page_size=1)
    assert result['facets'] == chronological['facets']
    assert result['weights'] == chronological['weights']
