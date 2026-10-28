"""TV inherits prepared musical choices and resolves only bounded visual editions."""
import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.onboarding_support import publish_personal_album, publish_personal_mix
from yt_playlist.rec import tv
from yt_playlist.web.app import create_app


def row(index=0, title='Prepared 0 (Official Music Video)', **values):
    return {'videoId': f'visual{index:05}', 'title': title, 'artists': [{'name': 'Artist'}],
            'duration_seconds': 200, **values}


def test_home_keeps_tv_entry_with_only_tv_prepared_and_no_mix_prototypes(store):
    from tests.test_tv_stream import video
    iid = store.identities.upsert_identity('TV account', 'cred', None, True)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('home_format', 'mixes')
    store.rec_proposals.put_proposals(tv.SURFACE, {
        'schema': tv.SCHEMA, 'key': 'independent-tv', 'at': 1, 'attempted': [], 'items': [video(0)]}, 1)
    assert store.rec_proposals.get_proposals('home_prepared') is None
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        html = client.get('/').text
        assert 'data-dash="tv"' in html and '>TV<span' in html
        assert 'class="tv-home tv-session"' in html
        assert 'Adjust TV parameters' in html
        assert client.get('/home/tv').json()['ready'] is True


@pytest.mark.parametrize('failed', [False, True])
def test_empty_and_failed_tv_states_remain_reachable_without_mix_prototypes(store, failed):
    iid = store.identities.upsert_identity('TV account', 'cred', None, True)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('home_format', 'albums')
    publish_personal_album(store)
    store.rec_proposals.put_proposals(tv.SURFACE, {
        'schema': tv.SCHEMA, 'key': 'independent-tv', 'at': 1, 'attempted': [],
        'items': [], 'state': 'no_sources'}, 1)
    store.rec_proposals.put_proposals('tv_preparation', {'failed': failed}, 1)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        html = client.get('/').text
        assert 'data-dash="tv"' in html and 'class="tv-home tv-session"' in html
        status = client.get('/home/tv').json()
        assert status['ready'] is False
        assert ('reconnecting' if failed else 'liked songs') in status['message']


class Videos(FakeClient):
    def __init__(self, results=None, details=None):
        super().__init__()
        self.results = results or [row()]
        self.details = details or {}
        self.searches, self.lookups = [], []

    def search(self, query, filter='songs'):
        self.searches.append((query, filter))
        self._catalog.update({t['videoId']: t for t in self.results})
        return self.results

    def get_song(self, video):
        self.lookups.append(video)
        return self.details.get(video, {})


@pytest.mark.parametrize('title,kind', [
    ('Song (Visualizer)', 'Visualizer'), ('Song visualiser', 'Visualizer'),
    ('Song (Live session)', 'Live session'), ('Song Official Music Video', 'Music video'),
    ('Song (Official Audio)', None), ('Song Lyric Video', None), ('Song', None)])
def test_visual_evidence(title, kind):
    assert tv.video_kind({'title': title}) == kind


def test_official_video_metadata_is_evidence_but_cannot_override_audio_title():
    assert tv.video_kind({'title': 'Song', 'videoType': 'MUSIC_VIDEO_TYPE_OMV'}) == 'Music video'
    assert tv.video_kind({'title': 'Song Official Audio', 'videoType': 'MUSIC_VIDEO_TYPE_OMV'}) is None


def test_resolver_rejects_wrong_recording_artist_stills_unknown_overlong_and_blocked(store):
    seeds = publish_personal_mix(store)
    rows = [row(1, artists=[{'name': 'Unrelated'}]), row(2, title='Unrelated Official Video'),
            row(3, title='Prepared 0 Official Audio'), row(4, duration_seconds=None),
            row(5, duration_seconds=901), row(6), row(7)]
    client = Videos(rows, {'visual00006': {'playabilityStatus': {'status': 'UNPLAYABLE'}}})
    # At most two detailed lookups per seed: narrow to the invalid edition and a valid fallback.
    assert not tv.resolve(client, seeds[:1], 15)
    client.results = rows[-2:]
    picks = tv.resolve(client, seeds[:1], 15)
    assert [t['video_id'] for t in picks] == ['visual00007']
    assert picks[0]['source_key'] == seeds[0]['key']
    assert all(kind == 'videos' for _, kind in client.searches)


def test_resolver_caps_remote_work_and_deduplicates_videos(store):
    seeds = publish_personal_mix(store)
    client = Videos([row(i, title=f'Prepared {i} Visualizer') for i in range(4)])
    picks = tv.resolve(client, seeds * 20, 15)
    assert len(picks) == tv.MAX_SEEDS
    assert len({t['video_id'] for t in picks}) == tv.MAX_SEEDS
    assert len(client.searches) == tv.MAX_SEEDS * len(tv.SEARCHES)
    assert len(client.lookups) == tv.MAX_SEEDS


def test_preferred_searches_rank_footage_ahead_of_stills_and_visualizers(store):
    seeds = publish_personal_mix(store)
    class Editions(Videos):
        def search(self, query, filter='songs'):
            self.searches.append((query, filter))
            if query.endswith('session'):
                return [row(3, title='Prepared 0 (Studio Session)')]
            return [row(0, title='Prepared 0 (Visualizer)'),
                    row(1, title='Prepared 0 (Official Music Video)', videoType='MUSIC_VIDEO_TYPE_ATV'),
                    row(2, title='Prepared 0 (Static Image)')]
    client = Editions()
    picked = tv.resolve(client, seeds[:1], 15)
    assert [item['video_id'] for item in picked] == ['visual00003']
    assert [query for query, _ in client.searches] == [f'Artist Prepared 0 {edition}' for edition in tv.SEARCHES]


@pytest.mark.parametrize('details', [
    {'musicVideoType': 'MUSIC_VIDEO_TYPE_ATV'}, {'author': 'Artist - Topic'},
    {'shortDescription': 'Provided to YouTube by a record company'},
    {'shortDescription': 'An audio only upload with a static image'},
])
def test_detailed_audio_evidence_overrules_misleading_visual_title(store, details):
    seeds = publish_personal_mix(store)
    client = Videos([row()], {'visual00000': {'videoDetails': details}})
    assert not tv.resolve(client, seeds[:1], 15)


def test_pool_uses_likes_unowned_artist_tracks_and_discovery_without_exposure(store, monkeypatch):
    seeds = publish_personal_mix(store)
    for seed in seeds:
        store.rec_model.record_like(seed['key'], 1000)
    fresh = [{'key': f'fresh-{i}', 'title': f'New {i}', 'artist': artist, 'video_id': f'new-{i}'}
             for i, artist in enumerate(['Artist', 'Discovery A', 'Artist', 'Discovery B'])]
    monkeypatch.setattr(tv.fresh_pool, 'pick', lambda *args, **kwargs: fresh)
    def forbidden(*args, **kwargs):
        raise AssertionError('TV must not render cards or log mix exposure')
    monkeypatch.setattr(tv.home_prepared, 'cards', forbidden)
    picked = tv.seed_tracks(store, 1000)
    assert [t['group'] for t in picked[:6]] == list(tv.GROUPS) * 2
    assert [t['key'] for t in picked if t['group'] == 'liked'] == [t['key'] for t in seeds]
    assert all(t['artist'] == 'Artist' for t in picked if t['group'] == 'your_artists')
    assert not set(t['key'] for t in picked if t['group'] != 'liked') & store.rec_query.library_keys()


def prepare_pool(store, client, now=1):
    seeds = publish_personal_mix(store, now=now)
    for seed in seeds:
        store.rec_model.record_like(seed['key'], now)
    while tv.prepare(store, client, now):
        pass
    return tv.cached(store)


def test_preparation_publishes_in_bounded_slices_and_recovers_after_failure(store, monkeypatch):
    seeds = [{'key': f'key-{i}', 'title': f'Prepared {i}', 'artist': 'Artist', 'video_id': str(i), 'group': tv.GROUPS[i % 3]}
             for i in range(15)]
    monkeypatch.setattr(tv, 'seed_tracks', lambda *_: seeds)
    client = Videos([row(i, title=f'Prepared {i} Visualizer') for i in range(15)])
    assert tv.prepare(store, client, 1000)
    first = tv.cached(store)
    assert len(first['attempted']) == tv.MAX_SEEDS and len(client.searches) == tv.MAX_SEEDS * len(tv.SEARCHES)
    def failed(*args, **kwargs): raise RuntimeError('offline')
    search = client.search
    client.search = failed
    with pytest.raises(RuntimeError, match='offline'): tv.prepare(store, client, 1001)
    assert tv.cached(store) == first
    assert store.rec_proposals.get_proposals('tv_preparation')['failed']
    client.search = search
    while tv.prepare(store, client, 1002):
        pass
    assert len(tv.cached(store)['attempted']) == 15
    assert not store.rec_proposals.get_proposals('tv_preparation')['failed']
    count = len(client.searches)
    assert not tv.prepare(store, client, 1004)
    assert len(client.searches) == count


def test_empty_home_refresh_keeps_available_prepared_videos_but_respects_feedback(store, monkeypatch):
    client = Videos()
    previous = prepare_pool(store, client)
    monkeypatch.setattr(tv, 'seed_tracks', lambda *_: [])
    assert not tv.prepare(store, client, 2)
    assert tv.cached(store)['items'] == previous['items']
    assert tv.queue(store, 2)
    for item in previous['items']:
        store.rec_model.record_dislike(item['source_key'], until=None, now=3)
    assert not tv.prepare(store, client, 3)
    assert not tv.queue(store, 3)


def test_search_cannot_publish_after_source_removal(store, monkeypatch):
    client = Videos()
    previous = prepare_pool(store, client)
    monkeypatch.setattr(tv, 'MAX_AGE', 1)
    assert tv.seed_tracks(store, 2)
    search = client.search
    def remove_source(*args, **kwargs):
        store.rec_proposals.put_proposals('home_tuned', {}, 2)
        return search(*args, **kwargs)
    client.search = remove_source
    assert tv.prepare(store, client, 2)
    assert tv.cached(store) == previous


def test_refill_continues_across_menu_changes_and_keeps_prepared_videos(store, monkeypatch):
    seeds = [{'key': f'key-{i}', 'title': f'Prepared {i}', 'artist': 'Artist',
              'group': 'liked'} for i in range(7)]
    candidates = [seeds[:6]]
    monkeypatch.setattr(tv, 'seed_tracks', lambda *_: candidates[0])
    client = Videos([row(i, title=f'Prepared {i} Visualizer') for i in range(7)])
    assert tv.prepare(store, client, 1000)
    first = tv.cached(store)
    assert len(first['items']) == tv.MAX_SEEDS
    # Reverse the ranking, add a candidate and remove a previously searched one.
    # None of those changes should restart every search or blank the serving pool.
    candidates[0] = list(reversed(seeds[1:]))
    client.results = [row(99, title='Unrelated Official Video')]
    assert tv.prepare(store, client, 1001)
    second = tv.cached(store)
    assert len(second['attempted']) == 5
    assert {t['source_key'] for t in second['items']} == {'key-1', 'key-2'}
    assert not tv.prepare(store, client, 1002)
    assert len(tv.cached(store)['attempted']) == 6
    assert all('Prepared 1 ' not in query and 'Prepared 2 ' not in query
               for query, _ in client.searches[tv.MAX_SEEDS * len(tv.SEARCHES):])


def test_precalculated_serving_parameters_generation_and_sidebar_launch(store, caplog):
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    store.settings.set_setting('last_sync_at', '1')
    client = Videos([row(i, title=f'Prepared {i} Visualizer') for i in range(4)])
    prepare_pool(store, client)
    searches = len(client.searches)
    app = create_app(store, lambda: {iid: client}, now_fn=lambda: 1)
    with TestClient(app, base_url='http://127.0.0.1') as web:
        assert web.get('/home/tv').json() == {'ready': True, 'max_minutes': 15}
        assert not client.created
        assert not web.post('/home/tv', data={'max_minutes': '2'}).json()['ready']
        assert web.post('/home/tv', data={'max_minutes': '15'}).json()['ready']
        result = web.post('/home/tv/play')
        assert result.status_code == 200, result.text
        result = result.json()
        assert len(result['items']) == 3  # artist cap applies to each draw
        playlist = store.playlists.get_playlist(result['playlist_id'])
        assert store.playlists.is_quarantined_playlist(playlist.scope_ref)
        assert store.playlists.get_playlist_groups()[playlist.scope_ref] == 'Generated'
        assert store.playlist_recipes.get_recipe(playlist.scope_ref)['media'] == 'video'
        assert web.get(f'/home/tv/playlist/{playlist.id}').json()['items'] == result['items']
        markup = web.get('/playlists').text
        assert '"is_video": true' in markup
        assert 'Play on TV' in web.get(f'/playlist/{playlist.id}').text
        assert web.post('/play', json={'url': f'https://music.youtube.com/playlist?list={playlist.ytm_playlist_id}'}).json() == {
            'ok': True, 'tv_url': f'/?tv_playlist={playlist.id}#tv'}
        event_url = f'/home/tv/playlist/{playlist.id}/event'
        video = result['items'][0]['video_id']
        assert web.post(event_url, data={'kind': 'player_error', 'video_id': video,
                                        'state': '5', 'position': '3.15'}).json() == {'ok': True}
        assert f'TV player error: playlist={playlist.id} video={video} code=5 position=3.150' in caplog.text
        assert store.history.play_events_since(0) == []
        assert all(at is None for tracks in store.charts.get_playlist_track_recency(include_generated=True).values()
                   for at in tracks)
        assert web.post(event_url, data={'kind': 'player_error', 'video_id': video,
                                        'state': 'bogus'}).status_code == 400
        assert web.post(event_url, data={'kind': 'player_error', 'video_id': 'not-in-playlist',
                                        'state': '5'}).status_code == 400
        assert web.post(event_url, data={'kind': 'play', 'video_id': video}).json() == {'ok': True}
        assert web.post(event_url, data={'kind': 'play', 'video_id': video}).json() == {'ok': True}
        plays = store.history.play_events_since(0)
        assert len(plays) == 1 and plays[0]['playlist_ytm_id'] == playlist.ytm_playlist_id
        assert 1 in store.charts.get_playlist_track_recency(include_generated=True)[playlist.id]
        assert all(at is None for at in store.charts.get_playlist_track_recency()[playlist.id])
        assert web.get('/home/tv').json()['ready']  # saving does not exhaust the pool
        assert web.post(event_url, data={'kind': 'ended', 'video_id': video, 'position': '200'}).status_code == 200
        assert web.post(event_url, data={'kind': 'play', 'video_id': 'not-in-playlist'}).status_code == 400
        assert web.post(event_url, data={'kind': 'play', 'video_id': video, 'position': 'nan'}).status_code == 400
        assert len(client.searches) == searches
        store.rec_model.record_feedback('for_you', 'artist:Artist', 'mute', now=1)
        assert not web.get('/home/tv').json()['ready']
        pending = web.post('/home/tv/play')
        assert pending.status_code == 202 and pending.json()['preparing']
        assert 'current TV choices' in pending.json()['message']
        assert web.post('/home/tv', data={'max_minutes': '0'}).status_code == 400
        assert web.post('/home/tv', data={'max_minutes': 'nan'}).status_code == 400
        assert web.get('/home/tv/playlist/999999').status_code == 404


def test_missing_pool_requests_background_preparation_without_a_retry_error(store, monkeypatch):
    app = create_app(store, lambda: {}, now_fn=lambda: 1)
    requests = []
    monkeypatch.setattr(app.state.ctx.rec_worker, 'trigger_tv_refresh', lambda: requests.append('tv'))
    with TestClient(app, base_url='http://127.0.0.1') as web:
        assert not web.get('/home/tv').json()['ready']
        response = web.post('/home/tv/play')
        assert response.status_code == 202
        assert response.json() == {'preparing': True}
        assert requests == ['tv', 'tv']
        assert tv.draw_number(store) == 0


def test_new_sessions_use_unseen_pool_videos_before_recent_sessions_and_keep_lane_balance(store):
    items = [{'video_id': f'visual{i:05}', 'title': f'Video {i}', 'artist': f'Artist {i}',
              'source_artist': f'Artist {i}', 'source_key': f'source-{i}', 'duration': 200,
              'kind': 'Music video', 'group': tv.GROUPS[i % 3]} for i in range(36)]
    store.rec_proposals.put_proposals(tv.SURFACE, {
        'schema': tv.SCHEMA, 'key': 'pool', 'at': 1, 'attempted': [], 'items': items}, 1)
    sessions = []
    for _ in range(3):
        picked = tv.queue(store, 1)
        assert len(picked) == tv.QUEUE_SIZE
        assert [t['group'] for t in picked] == list(tv.GROUPS) * 4
        sessions.append({t['video_id'] for t in picked})
        tv.record_draw(store, picked)
    assert len(set.union(*sessions)) == 36
    assert not sessions[0] & sessions[1] and not sessions[1] & sessions[2]
    # Once the whole pool has been used, older sessions get another turn.
    assert {t['video_id'] for t in tv.queue(store, 1)} == sessions[0]


def test_small_pools_still_change_the_first_video_on_each_new_session(store):
    prepare_pool(store, Videos([row(i, title=f'Prepared {i} Visualizer') for i in range(4)]))
    for _ in range(5):
        previous = tv.queue(store, 1)
        tv.record_draw(store, previous)
        current = tv.queue(store, 1)
        assert current[0]['video_id'] != previous[0]['video_id']
        assert set(t['video_id'] for t in current) - set(t['video_id'] for t in previous)


def test_refill_never_queues_the_current_video_when_every_alternative_failed(store):
    prepare_pool(store, Videos([row(i, title=f'Prepared {i} Visualizer') for i in range(4)]))
    items = tv.cached(store)['items']
    current = items[-1]['video_id']
    assert tv.queue(store, 1, current=current, exclude=[t['video_id'] for t in items[:-1]]) == []


def test_handoff_waits_for_extension_confirmation_and_never_assumes_success(store, monkeypatch):
    app = create_app(store, lambda: {}, now_fn=lambda: 1)
    frames = []
    async def command(frame, **kwargs):
        frames.append(frame)
        return 200, json.dumps({'ok': True, 'paused': True})
    monkeypatch.setattr(app.state.ctx.bridge, 'request_control', command)
    with TestClient(app, base_url='http://127.0.0.1') as web:
        assert web.post('/home/tv/takeover').json() == {'ok': True}
        assert frames == [{'type': 'pause-for-tv'}]
        async def unconfirmed(*args, **kwargs): return 200, '{"ok": true}'
        monkeypatch.setattr(app.state.ctx.bridge, 'request_control', unconfirmed)
        assert web.post('/home/tv/takeover').status_code == 409


def test_tv_length_cap_can_admit_long_video_without_changing_music_limit(store):
    from yt_playlist.library.playlist_generated import create_generated_playlist
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    client = Videos([row(duration_seconds=2100)])
    prepare_pool(store, client)
    assert not tv.queue(store, 1)
    store.settings.set_setting(tv.SETTING, '40')
    items = tv.queue(store, 1)
    assert len(items) == 1 and items[0]['duration'] == 2100
    with pytest.raises(ValueError, match='maximum track length'):
        create_generated_playlist(store, 'Music', items, client, 1, iid)
    app = create_app(store, lambda: {iid: client}, now_fn=lambda: 1)
    with TestClient(app, base_url='http://127.0.0.1') as web:
        result = web.post('/home/tv/play')
        assert result.status_code == 200, result.text
        assert result.json()['items'][0]['duration'] == 2100


def test_tv_tab_waits_for_real_mixes_and_follows_long_play(store):
    from html.parser import HTMLParser

    class Tabs(HTMLParser):
        def __init__(self, markup):
            super().__init__()
            self.labels = []
            self.in_tab = False
            self.feed(markup)

        def handle_starttag(self, tag, attrs):
            if tag == 'button' and dict(attrs).get('role') == 'tab':
                self.in_tab = True
                self.labels.append('')

        def handle_data(self, data):
            if self.in_tab:
                self.labels[-1] += data

        def handle_endtag(self, tag):
            if tag == 'button':
                self.in_tab = False

    iid = store.identities.upsert_identity('main', 'cred', None, True)
    store.settings.set_setting('last_sync_at', '1')
    with TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1),
                    base_url='http://127.0.0.1') as web:
        assert 'TV' not in [label.strip() for label in Tabs(web.get('/').text).labels]
        publish_personal_mix(store, now=1)
        markup = web.get('/').text
        labels = [label.strip() for label in Tabs(markup).labels]
        assert labels.index('Long play') + 1 == labels.index('TV')
        assert labels.index('TV') + 1 == labels.index('Discover')


def test_cache_expiry_parameters_and_bad_payloads_are_safe(store):
    for payload in [[], {'schema': tv.SCHEMA, 'key': 'k', 'at': 'bad'},
                    {'schema': 2, 'key': 'old-resolver', 'at': 1000, 'items': [], 'attempted': []},
                    {'schema': tv.SCHEMA, 'key': 'k', 'at': 1000, 'items': None},
                    {'schema': tv.SCHEMA, 'at': float('inf'), 'items': []}]:
        store.rec_proposals.put_proposals(tv.SURFACE, payload, 1000)
        assert tv.cached(store) is None
