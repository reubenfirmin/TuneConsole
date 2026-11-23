"""Home Discover serves prepared pools immediately and never fits models on a request."""
import re
from contextlib import ExitStack
from functools import wraps
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.home_fixture_support import home_page_bytecode, publish_cards, returning_listener, without_metadata_providers
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.repos.test_album_aliases import ALIAS, OLD, release
from tests.store_fixture_support import (
    bounded_consumer_schema, native_sql_calls, uncontended_file_consumer_store as store)
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.recommendation_invalidation import clear_model_display
from yt_playlist.integrations.youtube_music_access import YouTubeMusicAccess
from yt_playlist.rec import discover, fresh_pool, home_cache, home_cards, home_prepared, home_vibe, surfaces
from yt_playlist.web.app import create_app


pytestmark = pytest.mark.usefixtures('native_sql_calls')


@pytest.fixture(scope='module')
def _pristine_current_schema():
    from yt_playlist.core.migrations import current, regular

    with bounded_consumer_schema((('jazz', 'Jazz'), ('house', 'House')),
                                 snapshot_guards=False) as schema:
        assert schema.execute('PRAGMA query_only').fetchone()[0] == 1
        assert schema.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == current.VERSION
        assert regular.validate(schema) == len(regular.CHANGES)
        assert schema.execute('SELECT count(*) FROM recordings').fetchone()[0] == 0
        yield schema


@pytest.fixture(autouse=True)
def discovery_clients(monkeypatch, request):
    make_client = client_for
    with ExitStack() as clients:
        monkeypatch.setattr(request.module, 'client_for',
                            lambda *args, **kwargs: clients.enter_context(make_client(*args, **kwargs)))
        yield


@pytest.fixture(autouse=True)
def discovery_routes(monkeypatch, home_page_bytecode, without_metadata_providers):
    from fastapi import APIRouter
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_vibes, discovery, home

    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx)
        for module in (app_update, audio_vibes, discovery, home)])
    monkeypatch.setattr(webapp, 'build_bridge_route', lambda ctx: APIRouter())
    monkeypatch.setattr(webapp, '_install_idle_shutdown', lambda *args: None)
    templates = webapp.Jinja2Templates

    def compiled_templates(*args, **kwargs):
        result = templates(*args, **kwargs)
        result.env.bytecode_cache = home_page_bytecode
        return result

    monkeypatch.setattr(webapp, 'Jinja2Templates', compiled_templates)


def seed_songs(store, count=40, *, genres=('Jazz',)):
    # Discovery reads consume ranked output. The Fresh producer has its own
    # worker owners; retain all forty subjects and real typed pool publication.
    tracks = [{'key': f'song {i}|artist {i}', 'video_id': f'v{i}', 'title': f'Song {i}',
               'artist': f'Artist {i}', 'genre': genres[i % len(genres)], 'year': '2020'} for i in range(count)]
    seed_publication_subjects(store, tracks, discoveries=[track['key'] for track in tracks])
    publish_bundles(store, {'all': {'fresh': tracks}}, 2, fresh=tracks)


def prepare_client_state(store):
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    iid = store.accounts.upsert_account("main", None, True)
    assert home_prepared.prepare_discovery(store, 3) is not None
    # The page consumes the completed Fresh mix alongside the independent
    # discovery pool. Keep that real offer without preparing unrelated lanes.
    # Discovery still retains all forty ranked subjects. Its companion needs
    # only the four playable tracks required for a real saved-mix offer.
    fresh = fresh_pool.pick(store, 3, size=4)
    protos = [home_cards._proto(store, 'fresh', 'Fresh songs', fresh, 3)] if fresh else []
    for proto in protos:
        proto['recipe'] = {'model': 'fresh', 'lane': 'fresh'}
    result = home_cards.CardsResult(protos, [{'proto': proto, 'pool': None} for proto in protos],
                                   None, None, [], [], home_cards._epoch(store, 'cards'), None)
    publish_cards(store, result, 3)
    returning_listener(store, 'mixes', now=3)
    return iid


def client_for(store, busy="idle", *, prepare=True):
    iid = prepare_client_state(store) if prepare else store.accounts.get_master_account().id
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 3, background_work=False)
    app.state.ctx.rec_worker._model_active = busy == "rebuild"
    app.state.ctx.rec_worker._discovering = busy == "discovery"
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.mark.parametrize('retained', [True, False])
def test_album_choices_only_loads_radio_for_readiness(store, monkeypatch, retained):
    store.accounts.upsert_account('main', None, True)
    if retained:
        returning_listener(store, 'albums')
    client = client_for(store, prepare=False)
    calls = []

    def bootstrap():
        calls.append(True)
        if retained:
            raise WorkBudgetExceeded('controlled Radio read timeout')
        return {}

    monkeypatch.setattr(client.app.state.ctx.radio, 'bootstrap', bootstrap)
    response = client.get('/home/album-choices', headers={'HX-Request': 'true'})
    assert response.status_code == (200 if retained else 409)
    assert len(calls) == (0 if retained else 1)


@pytest.fixture
def discovery_songs(store):
    seed_songs(store)


@pytest.fixture
def empty_discovery_home(store):
    assert prepare_client_state(store) == 1


@pytest.fixture(autouse=True)
def discovery_costs(monkeypatch, record_property, request):
    totals = {}

    def timed(name, operation):
        @wraps(operation)
        def call(*args, **kwargs):
            started = time.perf_counter()
            try:
                return operation(*args, **kwargs)
            finally:
                values = totals.setdefault(name, [0, 0.0])
                values[0] += 1
                values[1] += time.perf_counter() - started
        return call

    for owner, attribute in ((request.module, 'seed_songs'), (request.module, 'client_for'),
            (fresh_pool, 'prepare'), (home_prepared, 'prepare'), (home_prepared, 'prepare_discovery'),
            (TestClient, 'request')):
        name = owner.__name__.rsplit('.', 1)[-1] + '_' + attribute
        monkeypatch.setattr(owner, attribute, timed(name, getattr(owner, attribute)))
    yield
    for name, (calls, seconds) in totals.items():
        record_property(name + '_calls', calls)
        record_property(name + '_seconds', seconds)


@pytest.mark.parametrize("busy", ["idle", "rebuild", "discovery"])
def test_persisted_recommendations_are_ready_when_discover_opens_and_survive_refresh(store, monkeypatch, busy, discovery_songs):
    store.discovery.upsert_discovered_album("album", "Artist", "Ready album", "2026", None, 1)
    store.discovery.upsert_discovered_artist("New artist", 1, [], [], None, 1)
    client = client_for(store, busy)
    assert all(row["offered_count"] == 0 for row in store.discovery.get_discovered_tracks())

    def forbidden(*args, **kwargs):
        raise AssertionError("Page reads must not discover, fit or rank candidates")

    monkeypatch.setattr(discover, "run_discovery", forbidden)
    monkeypatch.setattr(discover.ContentProjection, "fit", forbidden)
    monkeypatch.setattr(surfaces, "cold_candidates", forbidden)
    monkeypatch.setattr(fresh_pool, "prepare", forbidden)
    initial = client.get("/")
    assert initial.status_code == 200
    assert 'hx-get="/?panel=discover&amp;fragment=1"' in initial.text
    assert 'id="home-discovery"' not in initial.text
    monkeypatch.setattr(home_prepared, 'load', forbidden)
    for path in ("/?panel=discover&fragment=1", "/home/discovery", "/home/discovery"):
        response = client.get(path)
        assert response.status_code == 200
        for text in ("Ready album", "New artist", "Songs to discover", "Song 0"):
            assert text in response.text
        fragment = re.search(r'<div id="home-discovery".*', response.text, re.S)[0]
        assert fragment.count('hx-get="/home/discovery"') == int(busy != "idle")
        assert "Finding new music for you" not in fragment
        assert 'tile-badge' not in fragment
        assert 'Updating your recommendations' not in fragment
        assert 'fitting the playlists' not in fragment
        assert all(row["offered_count"] == 0 for row in store.discovery.get_discovered_tracks())
    assert store.discovery.get_discovered_albums()[0]["offered_count"] == 0
    assert store.discovery.get_discovered_artists()[0]["offered_count"] == 0


@pytest.mark.parametrize('same_release', [True, False], ids=['confirmed-alias', 'distinct-edition'])
def test_prepared_discovery_album_tiles_follow_confirmed_identity(store, same_release):
    album = release()
    for browse in (OLD, ALIAS):
        store.discovery.upsert_discovered_album(browse, 'All Them Witches', album['title'], '2015', None, 1)
    other = 'MPREb_distinct'
    store.discovery.upsert_discovered_album(other, 'Other artist', 'Other album', '2026', None, 1)
    client = client_for(store)
    retained = home_prepared.load_discovery(store)

    def visible_albums():
        response = client.get('/home/discovery')
        assert response.status_code == 200
        return re.findall(r'data-discovery-kind="album" data-discovery-id="([^"]+)"', response.text)

    before = visible_albums()
    assert len(before) == 3 and set(before) == {OLD, ALIAS, other}
    assert store.collection.album_id(OLD) != store.collection.album_id(ALIAS)
    assert store.collection.observe_album_representation(OLD, album, now=4)
    alternate = release()
    if not same_release:
        alternate['tracks'].reverse()
    assert store.collection.observe_album_representation(ALIAS, alternate, now=5)
    assert (store.collection.album_id(OLD) == store.collection.album_id(ALIAS)) is same_release
    assert home_prepared.load_discovery(store) == retained

    after = visible_albums()
    if same_release:
        canonical = store.collection.preferred_album_ids([OLD])[OLD]
        assert len(after) == 2 and set(after) == {canonical, other}
        view_id = str(uuid4())
        for browse in (OLD, ALIAS):
            assert client.post('/discovery/impressions', json={
                'kind': 'album', 'ids': [browse], 'view_id': view_id}).status_code == 204
        assert {row['browse_id']: row['offered_count'] for row in
                store.discovery.get_discovered_albums()} == {canonical: 1, other: 0}
    else:
        assert after == before


@pytest.mark.parametrize('same_name', [False, True], ids=['distinct-names', 'same-name-artists'])
def test_prepared_discovery_excludes_only_the_exact_muted_artist(store, monkeypatch, same_name):
    from tests.web_consumer_support import install_routes

    install_routes(monkeypatch, 'discovery', 'home', 'suggest')
    seed_songs(store, count=4)
    selected = store.artists.ensure_artist('Sam', provider='youtube', provider_id='UCsam')
    other_name = 'Sam' if same_name else 'Sally'
    other = store.artists.ensure_artist(other_name, provider='youtube', provider_id='UCother')
    assert selected != other
    for browse, name, identity in [('first', 'Sam', selected), ('second', other_name, other)]:
        store.discovery.upsert_discovered_artist(name, 1, [], [], None, 1, artist_id=identity)
        store.discovery.upsert_discovered_album(browse, name, browse, '2026', None, 1, artist_id=identity)

    client = client_for(store)
    publication = home_prepared.load_discovery(store)
    target, unaffected = {'first', f'@artist:{selected}'}, {'second', f'@artist:{other}'}

    def tiles():
        response = client.get('/home/discovery')
        assert response.status_code == 200
        return set(re.findall(r'data-discovery-id="([^"]+)"', response.text))

    item = f'artist:@artist:{selected}'
    store.rec_model.record_feedback('for_you', item, 'mute', until=2, now=1)
    assert target | unaffected <= tiles()
    saved = client.post('/recs/feedback', data={'surface': 'for_you', 'kind': 'mute', 'item': item})
    assert saved.status_code == 200
    assert store.rec_model.muted_album_ids(['first', 'second'], 3) == {'first'}
    after = tiles()
    assert unaffected <= after and target.isdisjoint(after)
    assert home_prepared.load_discovery(store) == publication


def test_song_only_pool_fills_discover_and_honors_current_exclusions(store, discovery_songs):
    store.rec_model.record_feedback("for_you", "song 0|artist 0", "dismiss", now=2)
    store.rec_model.record_feedback("for_you", "artist:Artist 1", "mute", now=2)
    store.tracks.upsert_track("v2", "Song 2", "Artist 2", None, None)
    client = client_for(store)
    response = client.get("/home/discovery")
    assert response.text.count('data-discovery-kind="track"') == 10
    for i in range(3):
        assert f'data-discovery-id="song {i}|artist {i}"' not in response.text
    assert "Finding new music" not in response.text
    assert "Browse your discovery pool" not in response.text

    key = re.search(r'data-discovery-id="([^"]+)"', response.text)[1]
    payload = {"kind": "track", "ids": [key], "view_id": str(uuid4())}
    assert client.post("/discovery/impressions", json=payload).status_code == 204
    assert client.post("/discovery/impressions", json=payload).status_code == 204
    assert store.discovery.discovered_tracks_by_keys([key])[key]["offered_count"] == 1


@pytest.mark.parametrize('same_artist,starter', [(True, False), (False, False), (True, True)],
                         ids=['acquired', 'same-name', 'starter'])
def test_prepared_new_artist_uses_current_library_credits(store, same_artist, starter):
    label, provider_id = 'New artist prospect', 'UCProspect'
    artist_id = store.artists.ensure_artist(label, provider='youtube', provider_id=provider_id)
    if starter:
        store.discovery.select_starter_artist(label, provider_id, '/static/favicon.svg', True, 1)
    else:
        store.discovery.upsert_discovered_artist(label, 1, [], [], '/static/favicon.svg', 1, artist_id=artist_id)
    client = client_for(store)
    path = '/home/new-artists'
    assert label in client.get(path).text
    published = home_prepared.load_discovery(store)
    assert [row['artist_id'] for row in published['discovery_artists']] == [artist_id]

    acquired_id = provider_id if same_artist else 'UCDifferent'
    store.tracks.upsert_track('acquired001', 'Acquired recording', label, duration_s=240,
                              library=True, credits=[{'name': label, 'id': acquired_id}])

    for path in ('/home/new-artists', '/home/discovery'):
        response = client.get(path)
        assert response.status_code == 200
        assert (label in response.text) == (starter or not same_artist)
    assert home_prepared.load_discovery(store) == published


def test_discovery_can_show_one_song_without_claiming_a_mix_is_ready(store):
    seed_songs(store, count=1)
    assert fresh_pool.pick(store, 3) == []  # A saved mix still needs at least four songs.
    response = client_for(store).get("/home/discovery")
    assert response.text.count('data-discovery-kind="track"') == 1
    assert "New recommendations will appear" not in response.text


def test_prepared_discovery_song_uses_a_playable_upload(store):
    seed_songs(store, count=1)
    client = client_for(store)
    shortlist = home_prepared.load_discovery(store)
    path = '/home/discovery'
    card = 'data-discovery-kind="track" data-discovery-id="song 0|artist 0"'

    with store.conn:
        store.conn.execute(
            "INSERT INTO track_sources(recording_id,video_id,available,duration_s,title,artist) "
            "SELECT recording_id,'discAlt0001',1,180,title,artist FROM track_sources WHERE video_id='v0'")
    preferred = client.get(path)
    assert preferred.status_code == 200 and card in preferred.text
    assert 'href="https://music.youtube.com/watch?v=v0"' in preferred.text

    with store.conn:
        store.conn.execute("UPDATE track_sources SET available=0 WHERE video_id='v0'")
    alternate = client.get(path)
    assert alternate.status_code == 200 and card in alternate.text
    assert 'href="/track/discAlt0001?' in alternate.text
    assert 'href="https://music.youtube.com/watch?v=discAlt0001"' in alternate.text

    with store.conn:
        store.conn.execute("UPDATE track_sources SET available=0 WHERE video_id='discAlt0001'")
    unavailable = client.get(path)
    assert unavailable.status_code == 200 and card not in unavailable.text
    assert home_prepared.load_discovery(store) == shortlist


def test_discovery_survives_model_cache_invalidation_and_rechecks_eligibility(store, monkeypatch, discovery_songs):
    store.discovery.upsert_discovered_album('retained-album', 'Artist', 'Retained album', '2026', None, 1)
    client = client_for(store, 'discovery')
    assert 'Retained album' in client.get('/home/discovery').text
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        clear_model_display(store.conn)
    assert store.rec_proposals.get_proposals('home_choice') is None

    def forbidden(*args, **kwargs):
        raise AssertionError('Refreshing discovery must not prepare or rank candidates')

    monkeypatch.setattr(home_prepared, 'prepare', forbidden)
    monkeypatch.setattr(home_prepared, 'prepare_discovery', forbidden)
    monkeypatch.setattr(discover, 'pick_discovered_albums', forbidden)
    monkeypatch.setattr(discover.ContentProjection, 'fit', forbidden)
    monkeypatch.setattr(surfaces, 'cold_candidates', forbidden)
    monkeypatch.setattr(fresh_pool, 'prepare', forbidden)
    # The journey gate may read the existing ranked Fresh reserve. Forbid
    # ranking itself, rather than its bounded, eligibility-filtered reader.
    monkeypatch.setattr(home_vibe, 'rank', forbidden)
    response = client.get('/home/discovery')
    assert 'Retained album' in response.text and 'Songs to discover' in response.text
    assert 'Finding new music for you' not in response.text

    store.rec_model.record_feedback('for_you', 'song 0|artist 0', 'dismiss', now=3)
    store.rec_model.record_feedback('for_you', 'artist:Artist 1', 'mute', now=3)
    store.discovery.delete_discovered_albums(['retained-album'])
    response = client.get('/home/discovery')
    assert 'Retained album' not in response.text
    assert 'data-discovery-id="song 0|artist 0"' not in response.text
    assert 'data-discovery-id="song 1|artist 1"' not in response.text


@pytest.mark.parametrize('with_jazz_album', [False, True])
def test_discovery_rechecks_each_tracks_genre_after_session_exclusion(store, with_jazz_album):
    seed_songs(store, count=6, genres=('Jazz', 'House'))
    if with_jazz_album:
        store.discovery.upsert_discovered_album('jazz-album', 'Jazz Artist', 'Jazz album',
                                                '2026', None, 1, genre='Jazz')
    client = client_for(store)
    retained = home_prepared.load_discovery(store)
    assert len(retained['discovery_tracks']) == 6
    before = client.get('/home/discovery')
    assert before.status_code == 200
    assert before.text.count('data-discovery-kind="track"') == 6
    assert ('Jazz album' in before.text) == with_jazz_album

    store.rec_model.set_session_weight('genre:jazz', 0, now=3, lo=0)
    after = client.get('/home/discovery')
    assert after.status_code == 200
    assert 'Jazz album' not in after.text
    assert after.text.count('data-discovery-kind="track"') == 3
    for index in range(6):
        assert (f'data-discovery-id="song {index}|artist {index}"' in after.text) == (index % 2 == 1)
    assert home_prepared.load_discovery(store) == retained


def test_discovery_publishes_even_when_mix_assembly_is_deferred(store, monkeypatch, discovery_songs):
    from yt_playlist.core.activity import WorkBudgetExceeded

    store.discovery.upsert_discovered_album('ready', 'Artist', 'Ready independently', '2026', None, 1)
    def exhausted(*args):
        raise WorkBudgetExceeded('Mix assembly is still catching up')

    monkeypatch.setattr(home_prepared, '_prepare', exhausted)
    assert home_prepared.prepare(store, 3) is None
    selection = home_prepared.discovery(store, None, 3)
    assert selection['discovery_albums'][0]['title'] == 'Ready independently'
    assert len(selection['discovery_tracks']) == 10


def test_new_discovery_pool_is_served_while_mix_projection_is_unchanged(store, discovery_songs):
    home_prepared.prepare(store, 3)
    old_mix_projection = home_prepared.load(store, 3)
    assert old_mix_projection and old_mix_projection['menus']
    assert home_prepared.load_discovery(store)['discovery_albums'] == []
    persisted_mix = home_cache.encoded(store, home_prepared.SURFACE)
    assert persisted_mix
    store.discovery.upsert_discovered_album('new-album', 'Artist', 'Fresh arrival', '2026', None, 4)
    assert home_prepared.prepare_discovery(store, 4) is not None
    assert home_cache.encoded(store, home_prepared.SURFACE) == persisted_mix
    selection = home_prepared.discovery(store, old_mix_projection, 4)
    assert [a['browse_id'] for a in selection['discovery_albums']] == ['new-album']


@pytest.mark.parametrize('bad_row', [None, {'browse_id': []},
    {'browse_id': 'bad', 'artist': []}, {'browse_id': 'bad', 'artist': 'Artist', 'genre': {}},
])
def test_corrupt_independent_discovery_pool_cannot_crash_a_refresh(store, bad_row, empty_discovery_home):
    client = client_for(store, prepare=False)
    store.rec_proposals.put_proposals(home_prepared.DISCOVERY_SURFACE, {
        'discovery_albums': [bad_row], 'discovery_artists': [], 'discovery_tracks': []}, 1)
    assert home_prepared.load_discovery(store) is None
    response = client.get('/home/discovery')
    assert response.status_code == 200


def test_missing_artist_art_is_queued_and_repaired_image_appears_without_reranking(store, monkeypatch):
    store.discovery.upsert_discovered_artist('New artist', 1, [], [], None, 1)
    client = client_for(store)
    queued = []
    lookups = []
    monkeypatch.setattr(client.app.state.ctx.rec_worker, 'trigger_artist_images', queued.append)

    def record_lookup(_access, name):
        lookups.append(name)
        return None

    def forbid_preparation(*args, **kwargs):
        raise AssertionError('Opening Discover must not prepare or rank candidates')

    monkeypatch.setattr(YouTubeMusicAccess, 'artist_thumbnail', record_lookup)
    monkeypatch.setattr(home_prepared, 'prepare_discovery', forbid_preparation)
    prepared = store.rec_proposals.get_proposals(home_prepared.DISCOVERY_SURFACE)
    assert prepared is not None
    response = client.get('/home/discovery')
    assert response.status_code == 200
    aid = store.discovery.get_discovered_artists()[0]['artist_id']
    assert queued == [[aid]]
    assert lookups == []
    assert 'New artist' in response.text
    thumbnail = 'http://portrait/new.jpg'
    assert thumbnail not in response.text

    store.discovery.record_artist_image(aid, thumbnail, 3)
    response = client.get('/home/discovery')
    assert response.status_code == 200
    assert thumbnail in response.text
    assert queued == [[aid]]
    assert lookups == []
    assert store.rec_proposals.get_proposals(home_prepared.DISCOVERY_SURFACE) == prepared


@pytest.mark.parametrize("busy", ["idle", "rebuild", "discovery"])
def test_empty_discovery_has_one_honest_state_and_no_idle_polling(store, busy, empty_discovery_home):
    response = client_for(store, busy, prepare=False).get("/home/discovery")
    assert response.status_code == 200
    assert response.text.count('role="status"') == 1
    assert response.text.count('hx-get="/home/discovery"') == int(busy != "idle")
    assert ("Finding new music for you" in response.text) == (busy != "idle")
    assert 'href="/discovery"' in response.text
