"""Account/upload evidence survives combination, missing contexts and identity corrections."""
import json
from types import SimpleNamespace

import pytest

from tests.conftest import FakeClient
from yt_playlist.core.bridge import BridgeError
from yt_playlist.library import live_plays, player_events, ratings, sync
from yt_playlist.rec import rec_params


def _client(items):
    return FakeClient(
        playlists=[{'playlistId': 'P', 'title': 'Playlist', 'count': len(items)}],
        tracks={'P': [{'videoId': video, 'title': 'Song', 'artists': [{'name': 'Artist'}],
                       'duration_seconds': 200, 'likeStatus': status}
                      for video, status in items]})


def _siblings(store):
    first = store.tracks.upsert_track('a', 'Song', 'Artist', 'Album', 200)
    store.tracks.upsert_track('b', 'Song', 'Artist', 'Album', 200)
    recording = store.tracks.catalog_subject(first)['recording_id']
    store.tracks.associate_youtube_item('b', recording, source='verified',
                                       evidence={'recording': 'same'}, now=1)
    return store.tracks.identity_key_for_video('a')


def _assert_feedback(store, key, status):
    assert (key in store.rec_model.recent_liked_keys()) == (status == 'LIKE')
    assert (key in store.rec_model.disliked_identity_keys()) == (status == 'DISLIKE')


@pytest.mark.parametrize('status', ['LIKE', 'DISLIKE'])
@pytest.mark.parametrize('reverse', [False, True])
def test_neutral_sibling_is_not_an_unlike_of_the_rated_upload(store, status, reverse):
    owner = store.identities.upsert_identity('Main', 'bridge', None, True)
    key = _siblings(store)
    items = [('a', status), ('b', 'INDIFFERENT')]
    if reverse:
        items.reverse()
    sync.sync_identity(store, owner, _client(items), now=10)
    _assert_feedback(store, key, status)
    assert store.tracks.item_rating(owner, 'a')['status'] == status
    assert store.tracks.item_rating(owner, 'b')['status'] == 'INDIFFERENT'
    theme = store.rec_surface.get_theme('artist:Artist')
    if status == 'LIKE':
        assert theme == rec_params.SOURCE_W_LIKE_SYNCED
    else:
        assert theme is None
    first_feedback = store.rec_model.disliked_with_ts() if status == 'DISLIKE' else [
        tuple(row) for row in store.conn.execute(
            "SELECT item_key,created_at FROM rec_feedback WHERE kind='like'")]
    sync.sync_identity(store, owner, _client(list(reversed(items))), now=20)
    _assert_feedback(store, key, status)
    assert store.rec_surface.get_theme('artist:Artist') == theme
    assert (store.rec_model.disliked_with_ts() if status == 'DISLIKE' else [
        tuple(row) for row in store.conn.execute(
            "SELECT item_key,created_at FROM rec_feedback WHERE kind='like'")]) == first_feedback
    # Only a genuine unlike of A withdraws A's contribution; no mutation propagates to B.
    sync.sync_identity(store, owner, _client([('b', 'INDIFFERENT'), ('a', 'INDIFFERENT')]), now=30)
    _assert_feedback(store, key, 'INDIFFERENT')
    assert store.tracks.item_rating(owner, 'a')['status'] == 'INDIFFERENT'


@pytest.mark.parametrize('status', ['LIKE', 'DISLIKE'])
@pytest.mark.parametrize('reverse', [False, True])
def test_neutral_account_is_not_an_unlike_from_another_account(store, status, reverse):
    first = store.identities.upsert_identity('Main', 'bridge', None, True)
    second = store.identities.upsert_identity('Other', 'bridge', 'other', False)
    observations = [(first, status), (second, 'INDIFFERENT')]
    if reverse:
        observations.reverse()
    for owner, value in observations:
        sync.sync_identity(store, owner, _client([('a', value)]), now=10)
    key = store.tracks.identity_key_for_video('a')
    _assert_feedback(store, key, status)
    theme = store.rec_surface.get_theme('artist:Artist')
    for owner, value in reversed(observations):
        sync.sync_identity(store, owner, _client([('a', value)]), now=20)
    _assert_feedback(store, key, status)
    assert store.rec_surface.get_theme('artist:Artist') == theme
    assert store.tracks.item_rating(first, 'a')['status'] == status
    assert store.tracks.item_rating(second, 'a')['status'] == 'INDIFFERENT'
    sync.sync_identity(store, first, _client([('a', 'INDIFFERENT')]), now=30)
    _assert_feedback(store, key, 'INDIFFERENT')


@pytest.mark.parametrize('scope', ['upload', 'account'])
@pytest.mark.parametrize('reverse', [False, True])
def test_explicit_conflicts_use_dislike_then_like_until_all_contributions_are_cleared(
        store, scope, reverse):
    first = store.identities.upsert_identity('Main', 'bridge', None, True)
    second = store.identities.upsert_identity('Other', 'bridge', 'other', False)
    key = _siblings(store)
    contributions = [(first, 'a', 'LIKE'),
                     (first if scope == 'upload' else second,
                      'b' if scope == 'upload' else 'a', 'DISLIKE')]
    if reverse:
        contributions.reverse()
    for owner, video, status in contributions:
        sync.sync_identity(store, owner, _client([(video, status)]), now=10)
    _assert_feedback(store, key, 'DISLIKE')
    for owner, video, status in reversed(contributions):
        sync.sync_identity(store, owner, _client([(video, status)]), now=20)
    _assert_feedback(store, key, 'DISLIKE')
    disliked_owner, disliked_video, _ = next(c for c in contributions if c[2] == 'DISLIKE')
    sync.sync_identity(store, disliked_owner, _client([(disliked_video, 'INDIFFERENT')]), now=30)
    _assert_feedback(store, key, 'LIKE')
    assert store.rec_surface.get_theme('artist:Artist') == rec_params.SOURCE_W_LIKE_SYNCED
    liked_owner, liked_video, _ = next(c for c in contributions if c[2] == 'LIKE')
    sync.sync_identity(store, liked_owner, _client([(liked_video, 'INDIFFERENT')]), now=40)
    _assert_feedback(store, key, 'INDIFFERENT')
    # Reliking still cannot replay a previously graduated recording.
    sync.sync_identity(store, liked_owner, _client([(liked_video, 'LIKE')]), now=50)
    assert store.rec_surface.get_theme('artist:Artist') == rec_params.SOURCE_W_LIKE_SYNCED


@pytest.mark.parametrize('status', ['LIKE', 'DISLIKE'])
@pytest.mark.parametrize('unavailable', ['partial', 'missing', 'inactive'])
def test_unavailable_account_contributions_remain_until_authoritatively_withdrawn(
        store, monkeypatch, status, unavailable):
    first = store.identities.upsert_identity('Main', 'bridge', None, True)
    second = store.identities.upsert_identity('Other', 'bridge', 'other', False)
    sync.sync_identity(store, first, _client([('a', status)]), now=10)
    key = store.tracks.identity_key_for_video('a')
    if unavailable == 'partial':
        client = _client([('a', 'INDIFFERENT')])
        original = client.get_playlist
        client.get_playlist = lambda *args, **kwargs: {
            **original(*args, **kwargs), 'continuations': [{'unread': 'page'}]}
        sync.sync_identity(store, first, client, now=20)
    elif unavailable == 'missing':
        client = _client([])

        def offline(**kwargs):
            raise BridgeError('offline')

        client.get_library_playlists = offline
        monkeypatch.setattr(sync, 'with_retry', lambda fn, **kwargs: fn())
        sync.sync_identity(store, first, client, now=20)
    else:
        with store.conn:
            store.conn.execute('UPDATE identities SET active=0 WHERE id=?', (first,))
    sync.sync_identity(store, second, _client([('a', 'INDIFFERENT')]), now=30)
    _assert_feedback(store, key, status)
    assert store.tracks.item_rating(first, 'a')['status'] == status
    with store.conn:
        store.conn.execute('UPDATE identities SET active=1 WHERE id=?', (first,))
    sync.sync_identity(store, first, _client([('a', 'INDIFFERENT')]), now=40)
    _assert_feedback(store, key, 'INDIFFERENT')


def test_api_native_and_player_paths_share_account_and_exact_upload_reconciliation(store):
    first = store.identities.upsert_identity('Main', 'bridge', None, True)
    second = store.identities.upsert_identity('Other', 'bridge', 'other', False)
    key = _siblings(store)
    calls = []
    api = SimpleNamespace(rate_song=lambda *args: calls.append(args))
    ratings.set_rating(store, first, 'a', 'DISLIKE', api, 10)
    ratings.set_rating(store, second, 'b', 'LIKE', api, 20)
    _assert_feedback(store, key, 'DISLIKE')
    assert calls == [('a', 'DISLIKE'), ('b', 'LIKE')]
    assert store.tracks.pending_action_ratings(second) == []
    # A native unlike belongs to A, and exposes the retained like of B.
    ctx = SimpleNamespace(store=store)
    event = {'kind': 'rate', 'url': 'https://music.youtube.com/youtubei/v1/like/removelike',
             'body': json.dumps({'target': {'videoId': 'a'}}), 'brandId': '',
             'phase': 'settled', 'success': True, 'httpStatus': 200}
    assert player_events.handle_player_event(ctx, event, 30)
    _assert_feedback(store, key, 'LIKE')
    assert store.tracks.item_rating(second, 'b')['status'] == 'LIKE'
    assert store.tracks.pending_action_ratings(first) == []
    # Neither a neutral player readout nor a stale pre-API dislike undoes that result.
    message = {'title': 'Song', 'artist': 'Artist', 'videoId': 'a', 'brandId': '',
               'likeStatus': 'DISLIKE'}
    live_plays.handle_play_event(ctx, message, 31)
    _assert_feedback(store, key, 'LIKE')
    # A real native dislike on B wins, while A's exact neutral remains unchanged.
    event.update(url='https://music.youtube.com/youtubei/v1/like/dislike',
                 body=json.dumps({'target': {'videoId': 'b'}}), brandId='other')
    assert player_events.handle_player_event(ctx, event, 40)
    _assert_feedback(store, key, 'DISLIKE')
    assert store.tracks.item_rating(first, 'a')['status'] == 'INDIFFERENT'
    event['url'] = 'https://music.youtube.com/youtubei/v1/like/removelike'
    assert player_events.handle_player_event(ctx, event, 50)
    _assert_feedback(store, key, 'INDIFFERENT')


def test_live_player_like_cannot_override_another_accounts_dislike(store):
    first = store.identities.upsert_identity('Main', 'bridge', None, True)
    second = store.identities.upsert_identity('Other', 'bridge', 'other', False)
    key = _siblings(store)
    sync.sync_identity(store, first, _client([('a', 'DISLIKE')]), now=10)
    ctx = SimpleNamespace(store=store)
    message = {'title': 'Song', 'artist': 'Artist', 'videoId': 'b', 'brandId': 'other',
               'likeStatus': 'LIKE'}
    live_plays.handle_play_event(ctx, message, 20)
    _assert_feedback(store, key, 'DISLIKE')
    assert store.tracks.item_rating(second, 'b')['status'] == 'LIKE'
    message['likeStatus'] = 'INDIFFERENT'
    live_plays.handle_play_event(ctx, message, 21)
    assert store.tracks.item_rating(second, 'b')['status'] == 'LIKE'
    ratings.record_confirmed(store, first, 'a', 'INDIFFERENT', 30)
    _assert_feedback(store, key, 'LIKE')


@pytest.mark.parametrize('status', ['LIKE', 'DISLIKE'])
@pytest.mark.parametrize('partial', [False, True])
def test_reassociation_moves_only_the_rated_uploads_feedback_without_replaying_learning(
        store, status, partial):
    owner = store.identities.upsert_identity('Main', 'bridge', None, True)
    old_key = _siblings(store)
    target = store.tracks.upsert_track('c', 'Other song', 'Other artist', 'Album', 200)
    target_recording = store.tracks.catalog_subject(target)['recording_id']
    target_key = store.tracks.identity_key_for_video('c')
    ratings.record_confirmed(store, owner, 'a', status, 10)
    ratings.record_confirmed(store, owner, 'b', 'INDIFFERENT', 11)
    original_theme = store.rec_surface.get_theme('artist:Artist')
    raw = store.tracks.item_rating(owner, 'a')
    store.tracks.associate_youtube_item('a', target_recording, source='corrected',
                                       evidence={'review': 'different recording'}, now=20)
    _assert_feedback(store, old_key, 'INDIFFERENT')
    _assert_feedback(store, target_key, status)
    if not partial:
        store.tracks.associate_youtube_item('b', target_recording, source='verified',
                                           evidence={'review': 'same recording'}, now=21)
    assert store.tracks.item_rating(owner, 'a') == raw
    assert store.rec_surface.get_theme('artist:Artist') == original_theme
    assert store.rec_surface.get_theme('artist:Other artist') is None
    sync.sync_identity(store, owner, _client([('a', status), ('b', 'INDIFFERENT')]), now=30)
    _assert_feedback(store, target_key, status)
    assert store.rec_surface.get_theme('artist:Other artist') is None
    ratings.record_confirmed(store, owner, 'a', 'INDIFFERENT', 40)
    _assert_feedback(store, target_key, 'INDIFFERENT')
    ratings.record_confirmed(store, owner, 'a', status, 50)
    # Correcting identity is not a second positive action to graduate on the new recording.
    assert store.rec_surface.get_theme('artist:Other artist') is None


def test_sync_confirmation_keeps_pending_live_action_timestamp_and_provenance(store):
    owner = store.identities.upsert_identity('Main', 'bridge', None, True)
    ratings.record_confirmed(store, owner, 'a', 'LIKE', 10)
    assert store.tracks.identity_key_for_video('a') is None
    sync.sync_identity(store, owner, _client([('a', 'LIKE')]), now=20)
    key = store.tracks.identity_key_for_video('a')
    assert store.rec_model.recent_liked_with_ts() == [(key, 10)]
    assert store.rec_surface.get_theme('artist:Artist') == rec_params.SOURCE_W_LIKE
    assert store.tracks.item_rating(owner, 'a') == {
        'status': 'LIKE', 'source': 'api', 'observed_at': 10}


@pytest.mark.parametrize('reverse', [False, True])
def test_partial_reassociation_reveals_retained_like_and_moves_dislike(store, reverse):
    owner = store.identities.upsert_identity('Main', 'bridge', None, True)
    old_key = _siblings(store)
    target = store.tracks.upsert_track('c', 'Other song', 'Other artist', 'Album', 200)
    target_recording = store.tracks.catalog_subject(target)['recording_id']
    target_key = store.tracks.identity_key_for_video('c')
    observations = [('a', 'DISLIKE'), ('b', 'LIKE')]
    if reverse:
        observations.reverse()
    for video, status in observations:
        ratings.record_confirmed(store, owner, video, status, 10)
    _assert_feedback(store, old_key, 'DISLIKE')
    store.tracks.associate_youtube_item('a', target_recording, source='corrected',
                                       evidence={'review': 'different recording'}, now=20)
    _assert_feedback(store, old_key, 'LIKE')
    _assert_feedback(store, target_key, 'DISLIKE')
    assert store.tracks.item_rating(owner, 'a')['status'] == 'DISLIKE'
    assert store.tracks.item_rating(owner, 'b')['status'] == 'LIKE'
    assert store.rec_surface.get_theme('artist:Other artist') is None
    assert store.tracks.pending_action_ratings(owner) == []


def test_live_rating_without_exact_upload_does_not_invent_account_evidence(store):
    owner = store.identities.upsert_identity('Main', 'bridge', None, True)
    key = _siblings(store)
    sync.sync_identity(store, owner, _client([('a', 'DISLIKE')]), now=10)
    live_plays.handle_play_event(SimpleNamespace(store=store), {
        'title': 'Song', 'artist': 'Artist', 'likeStatus': 'LIKE', 'brandId': ''}, 20)
    _assert_feedback(store, key, 'DISLIKE')
    assert store.conn.execute('SELECT COUNT(*) FROM item_ratings').fetchone()[0] == 1


def _learning_state(store):
    # Check the complete ledger and permanent weights: coherent item selection can
    # attribute repeated learning to either artist, so destination-only checks miss it.
    return {table: [tuple(row) for row in store.conn.execute(f'SELECT * FROM {table} ORDER BY 1')]
            for table in ('rec_theme', 'rec_weights', 'rec_grad_log')}


@pytest.mark.parametrize('status', ['LIKE', 'DISLIKE'])
@pytest.mark.parametrize('destination_first', [False, True])
@pytest.mark.parametrize('partial', [False, True])
def test_reassociation_merges_established_feedback_by_earliest_age_without_renewal(
        store, status, destination_first, partial):
    owner = store.identities.upsert_identity('Main', 'bridge', None, True)
    source = store.tracks.upsert_track('source', 'Source', 'Source artist', 'A', 200)
    old_recording = store.tracks.catalog_subject(source)['recording_id']
    old_key = store.tracks.identity_key_for_video('source')
    if partial:
        store.tracks.upsert_track('sibling', 'Source', 'Source artist', 'A', 200)
        store.tracks.associate_youtube_item('sibling', old_recording, source='verified',
                                           evidence={'recording': 'same'}, now=1)
    target = store.tracks.upsert_track('destination', 'Destination', 'Target artist', 'B', 200)
    target_recording = store.tracks.catalog_subject(target)['recording_id']
    key = store.tracks.identity_key_for_video('destination')
    videos = ['destination', 'source'] if destination_first else ['source', 'destination']
    ratings.record_confirmed(store, owner, videos[0], status, 10)
    ratings.record_confirmed(store, owner, videos[1], status, 20)
    first_key = store.tracks.identity_key_for_video(videos[0])
    expected = tuple(store.conn.execute(
        'SELECT created_at,until FROM rec_feedback WHERE item_key=? AND kind=?',
        (first_key, status.lower())).fetchone())
    learning = _learning_state(store)
    raw = {video: store.tracks.item_rating(owner, video) for video in videos}

    store.tracks.associate_youtube_item('source', target_recording, source='corrected',
                                       evidence={'review': 'same recording'}, now=30)

    feedback = lambda: tuple(store.conn.execute(
        'SELECT created_at,until FROM rec_feedback WHERE item_key=? AND kind=?',
        (key, status.lower())).fetchone())
    assert feedback() == expected
    assert _learning_state(store) == learning
    assert {video: store.tracks.item_rating(owner, video) for video in videos} == raw
    if status == 'LIKE':
        assert store.conn.execute("SELECT created_at FROM rec_feedback WHERE item_key=? AND kind='like_grad'",
                                  (key,)).fetchone()[0] == 10
    if not partial:
        assert not store.conn.execute('SELECT 1 FROM rec_feedback WHERE item_key=?', (old_key,)).fetchone()
    sync.sync_identity(store, owner, _client([(video, status) for video in reversed(videos)]), now=40)
    assert feedback() == expected
    assert _learning_state(store) == learning


@pytest.mark.parametrize('partial', [False, True])
def test_masked_reassociated_like_keeps_its_guard_and_never_replays_learning(store, partial):
    owner = store.identities.upsert_identity('Main', 'bridge', None, True)
    source = store.tracks.upsert_track('rated-upload', 'Source', 'Artist A', 'A', 200)
    if partial:
        store.tracks.upsert_track('neutral-sibling', 'Source', 'Artist A', 'A', 200)
        store.tracks.associate_youtube_item(
            'neutral-sibling', store.tracks.catalog_subject(source)['recording_id'],
            source='verified', evidence={'recording': 'same'}, now=1)
    target = store.tracks.upsert_track('disliked-upload', 'Destination', 'Artist B', 'B', 200)
    target_recording = store.tracks.catalog_subject(target)['recording_id']
    key = store.tracks.identity_key_for_video('disliked-upload')
    ratings.record_confirmed(store, owner, 'rated-upload', 'LIKE', 10)
    if partial:
        ratings.record_confirmed(store, owner, 'neutral-sibling', 'INDIFFERENT', 11)
    ratings.record_confirmed(store, owner, 'disliked-upload', 'DISLIKE', 20)
    learning = _learning_state(store)
    weights = store.rec_model.get_weights(now=40)
    raw = store.tracks.item_rating(owner, 'rated-upload')

    store.tracks.associate_youtube_item('rated-upload', target_recording, source='corrected',
                                       evidence={'review': 'different recording'}, now=30)

    _assert_feedback(store, key, 'DISLIKE')
    assert store.conn.execute("SELECT created_at FROM rec_feedback WHERE item_key=? AND kind='like_grad'",
                              (key,)).fetchone()[0] == 10
    assert store.tracks.item_rating(owner, 'rated-upload') == raw
    assert _learning_state(store) == learning
    # Removing the masking dislike exposes retained evidence, with no new like action.
    ratings.record_confirmed(store, owner, 'disliked-upload', 'INDIFFERENT', 40)
    _assert_feedback(store, key, 'LIKE')
    assert store.rec_model.recent_liked_with_ts() == [(key, 10)]
    for moment, status in [(50, 'LIKE'), (60, 'INDIFFERENT'), (70, 'LIKE'), (80, 'LIKE')]:
        sync.sync_identity(store, owner, _client([('rated-upload', status),
                                                ('disliked-upload', 'INDIFFERENT')]), now=moment)
        assert _learning_state(store) == learning
        assert store.rec_model.get_weights(now=40) == weights
