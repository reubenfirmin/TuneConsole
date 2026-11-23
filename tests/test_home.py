from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from tests.home_fixture_support import (
    home_page_bytecode, home_pages_only, publish_menu, returning_listener, without_metadata_providers,
)
from tests.onboarding_support import publish_live_mix_inputs
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.store_fixture_support import (
    bounded_consumer_schema, initialize_consumer, mock_snapshot_coordinator, reuse_reporting_reader,
)
from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient, _track


pytestmark = pytest.mark.usefixtures('home_pages_only', 'without_metadata_providers', 'native_sql_calls')


@pytest.fixture(scope='module')
def home_page_schema():
    with bounded_consumer_schema((('house', 'House'), ('techno', 'Techno')),
                                 snapshot_guards=False) as schema:
        yield schema


@pytest.fixture
def store(request, tmp_path, home_page_schema, monkeypatch):
    # Only taxonomy/subgenre owners require the complete curated vocabulary.
    full_catalog = request.node.name in {
        'test_home_genres_taxonomy', 'test_fingerprint_subgenres_attached_and_deduped',
        'test_fingerprint_add_reaches_zero_play_niche'}
    schema = request.getfixturevalue('_pristine_current_schema') if full_catalog else home_page_schema
    database = Store(tmp_path / 'home-pages.db')
    with closing(database.conn):
        # These pages consume admitted SQL; snapshot races and power-loss
        # durability belong below their rendering and navigation boundary.
        database.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(database, schema, snapshot_guards=False)
        database.conn.execute('PRAGMA wal_autocheckpoint=0')
        with mock_snapshot_coordinator(database, monkeypatch), reuse_reporting_reader(database, monkeypatch):
            yield database


def _client(store, now=1000.0, *, prepare=True):
    iid = store.accounts.upsert_account("main", None, True)
    if prepare:
        # Page consumers start with completed worker output. Ranking and Home
        # preparation have their own owners in test_home_prepared.
        publish_menu(store, now, prefix='baseline', tracks_per_card=1)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now)
    return TestClient(app, base_url="http://127.0.0.1")


def _publish_existing_listener_mix(store):
    from yt_playlist.rec import taste_analysis

    returning_listener(store, 'mixes', now=1000)
    publish_live_mix_inputs(store)
    taste_analysis.prepare_library_summary(store, 1000)


@pytest.mark.parametrize('route', ['/?panel=mixes', '/home/cards'])
def test_live_fresh_uses_published_genre_and_era_summary(store, monkeypatch, route):
    import re

    from yt_playlist.rec import recipes, taste_analysis

    client = _client(store, prepare=False)
    returning_listener(store, 'mixes', now=1000)
    tracks = publish_live_mix_inputs(store, tracks_per_lane=12)
    account = store.accounts.get_master_account().id
    store.history.record_history_plays(account, 900, [tracks[0]['key']], source_timezone='UTC')
    summary = taste_analysis.prepare_library_summary(store, 1000)
    assert summary['breadth']['families']

    def reread_history(*args, **kwargs):
        pytest.fail('Home already loaded the published genre and era summary')

    monkeypatch.setattr(recipes, 'taste_breadth', reread_history)
    monkeypatch.setattr(recipes, 'era_distribution', reread_history)
    response = client.get(route)
    assert response.status_code == 200
    cards = dict(re.findall(r'<section[^>]+id="gen-([\w-]+)"[^>]*>(.*?)</section>', response.text, re.S))
    assert set(cards) == {'wheelhouse', 'explore', 'fresh', 'comfort'}
    videos = {lane: re.findall(r'data-vid="([^"]+)"', card) for lane, card in cards.items()}
    assert all(len(items) == 12 for items in videos.values())
    assert len({video for items in videos.values() for video in items}) == 48
    assert set(videos['fresh']) == {track['video_id'] for track in tracks if track['video_id'].startswith('live-fresh-')}


def test_home_visit_ticks_card_rotation_but_previews_dont(store):
    """A Home visit advances the visible cards once; steer/breadth previews and the
    feed fragment re-render the same epoch without ticking, so tuning your taste never churns cards."""
    returning_listener(store, 'mixes', now=1000)
    c = _client(store)
    dao = store.rec_ledger
    assert dao.card_views("wheelhouse") == 0
    c.get("/")
    c.get("/")
    assert dao.card_views("wheelhouse") == 2 and dao.card_views("new_artists") == 0
    c.post("/home/breadth", data={"breadth_bias": "0.5"})  # previews must not advance rotation
    c.post("/home/steer", data={"axis": "genre:Rock", "weight": "1.5"})
    c.get("/home/feed")
    c.get("/home/fresh")                                   # lazy re-fetch (e.g. from a steer), read-only
    assert dao.card_views("wheelhouse") == 2 and dao.card_views("fresh") == 2


@pytest.mark.parametrize('newer_menu', [False, True])
def test_home_sends_cards_while_menu_persistence_is_blocked(store, monkeypatch, newer_menu):
    import re
    import socket
    from threading import Event
    from urllib.request import urlopen

    import uvicorn

    from tests.live_server import LiveServer
    from yt_playlist.rec import choice_evidence

    returning_listener(store, 'mixes', now=1000)
    publish_live_mix_inputs(store)
    app = _client(store, prepare=False).app
    surface = 'choice_window:mixes'
    newer = {'opportunity_id': 'newer-offer', 'window': '2'}
    started, release, completed = Event(), Event(), Event()
    save = store.rec_proposals.put_proposals

    def hold_save(name, value, now):
        if name != surface:
            return save(name, value, now)
        started.set()
        assert release.wait(10)
        save(name, value, now)
        completed.set()

    monkeypatch.setattr(store.rec_proposals, 'put_proposals', hold_save)
    with socket.create_server(('127.0.0.1', 0)) as listener:
        port = listener.getsockname()[1]
        config = uvicorn.Config(app, log_config=None, lifespan='off', ws='none', http='h11')
        with LiveServer(uvicorn.Server(config), sockets=[listener]):
            try:
                with urlopen(f'http://127.0.0.1:{port}/', timeout=5) as response:
                    assert response.status == 200
                    page = response.read()
                    assert b'id="home-mode-cards"' in page
                    assert page.count(b'class="gen-card card"') == 4
                    offers = re.findall(rb'data-choice-opportunity="([^"]+)"', page)
                    assert len(offers) == 4 and len(set(offers)) == 1
                    displayed = offers[0].decode()
                assert started.wait(5)
                assert store.rec_ledger.card_views('cards') == 0
                assert store.rec_proposals.get_proposals(surface) is None
                if newer_menu:
                    save(surface, newer, 1001)
            finally:
                release.set()
            assert completed.wait(5)

    assert store.rec_proposals.get_proposals(surface) == (
        newer if newer_menu else {'opportunity_id': displayed, 'window': '0'})
    offer = choice_evidence.opportunity(store, displayed)
    assert offer['format'] == 'mixes' and len(offer['options']) == 4
    assert store.rec_ledger.card_views('cards') == 1


def test_home_is_default_route(store):
    c = _client(store, prepare=False)
    r = c.get("/")
    assert r.status_code == 200
    assert 'id="home"' in r.text          # Home shell marker
    assert "home-status" in r.text         # live status card is present
    assert "Extension connected" not in r.text  # connection alone is not useful Home content
    assert "Library synced" not in r.text  # freshness line only appears after a first sync
    assert 'id="arrival-title"' in r.text
    assert 'Welcome to TuneConsole' in r.text
    assert 'class="festival-map"' in r.text
    assert 'id="home-mode-cards"' not in r.text
    assert "homeStatus()" in r.text
    assert "Sync now" not in r.text and "Full sync" not in r.text
    assert "Sync plays" not in r.text and "sync-toggle" not in r.text
    feed = c.get("/home/feed")
    assert feed.status_code == 409
    assert 'id="home-feed"' not in feed.text

    store.settings.set_setting("last_sync_at", "1000")
    html = c.get("/").text
    assert "Library synced" not in html and "not yet" not in html
    assert "hs-refresh" not in html


def test_next_home_visit_expires_a_day_old_theme_menu(store):
    from tests.mode_fixtures import seed_modes
    from tests.home_fixture_support import returning_listener
    seed_modes(store,1)
    returning_listener(store, 'mixes', now=1000)
    from yt_playlist.rec import rec_params
    from yt_playlist.rec.home_prepared import MENU_MAX_AGE_S
    from yt_playlist.rec.home_cards import _epoch
    rec_params.set_param(store, "erosion_view_cap", 10)
    dao = store.rec_ledger
    dao.bump_card_view("cards", 1000.0)
    store.modes.log_impressions(0, [("wheelhouse", 1)], 1000.0)
    clock = [1000.0 + MENU_MAX_AGE_S - 1]
    iid = store.accounts.upsert_account('main', None, True)
    publish_menu(store, clock[0], tracks_per_card=1)
    client = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: clock[0]),
                        base_url='http://127.0.0.1')
    assert client.get("/").status_code == 200
    assert _epoch(store, "cards") == 0
    clock[0] += 1
    assert client.get("/").status_code == 200
    assert _epoch(store, "cards") == 1

    # Fragment previews do not advance the new menu, even if its previous menu was old.
    views = dao.card_views("cards")
    assert client.get("/home/cards").status_code == 200
    assert dao.card_views("cards") == views


def test_home_does_not_call_unheard_saved_albums_a_rediscovery(store):
    store.settings.set_setting("last_sync_at", "1.0")          # Rediscover only renders on a synced Home
    store.settings.set_setting("onboard_dismissed", "1")       # graduated user: testing the normal feed, not onboarding
    store.collection.add_saved_album({"browse": "MPREb_x", "title": "Kind of Blue", "artist": "Miles Davis",
                                      "year": "1959", "type": "Album", "thumbnail": "http://img/x.jpg"})
    c = _client(store)
    html = c.get("/").text
    assert '<section id="rediscover">' not in html
    from yt_playlist.rec import home_prepared
    prepared = store.rec_proposals.get_proposals(home_prepared.SURFACE)
    assert prepared['rediscover_albums'] == []


def test_revisit_shows_full_current_playlist_in_saved_order(store):
    from html import unescape
    import re
    from tests.onboarding_support import publish_personal_mix
    from yt_playlist.rec import home_prepared

    iid = store.accounts.upsert_account("main", None, True)
    pid = store.playlists.upsert_playlist(iid, "quiet-list", "Quiet mix", 9, None, 1000)
    seed_publication_subjects(store, [{'key': f'song {i}|artist', 'video_id': f'v{9-i}',
        'title': f'Song {i}', 'artist': 'Artist'} for i in range(8)])
    with store.conn:
        store.conn.execute("UPDATE track_sources SET thumbnail='/static/icon.png'")
    tracks = [row[0] for row in store.conn.execute('SELECT id FROM track_sources ORDER BY id')]
    from tests.conftest import seed_catalog_track
    tracks.append(seed_catalog_track(store, "offline|artist", "Offline song", "Artist"))
    store.playlists.set_playlist_tracks(pid, tracks)
    # Revisit opens with two saved playlists (or another sufficiently useful
    # collection). A lone playlist is still below the real output threshold.
    other = store.playlists.upsert_playlist(iid, 'another-quiet-list', 'Second quiet mix', 9, None, 1000)
    store.playlists.set_playlist_tracks(other, list(reversed(tracks)))
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    # Exercise the real Revisit selector and rendering, independently of the
    # unrelated mix/discovery preparation pipeline.
    home_prepared.prepare_revisit(store, 1000)
    client = _client(store, prepare=False)
    # Prepared Revisit output cannot skip its journey predecessor. Dismissing
    # onboarding alone does not establish earlier listening or complete setup.
    assert not client.get('/home/growth').json()['ready_areas']['rediscover']
    assert 'id="onboarding-feed"' in client.get('/').text
    publish_personal_mix(store)
    returning_listener(store, 'mixes', now=1000)
    assert client.get('/home/growth').json()['ready_areas']['rediscover']
    assert client.get('/home/onboard/status').json()['setup']['complete']
    assert '<section id="rediscover">' not in client.get('/').text
    revisit = client.get('/?panel=rediscover&fragment=1')
    assert revisit.status_code == 200
    html = revisit.text
    section = html.split('<section id="rediscover">', 1)[1].split('</section>', 1)[0]
    table = re.search(r'<table class="track-table redis-tracks" aria-label="Quiet mix tracks".*?</table>',
                      section, re.S).group()
    rows = re.findall(r'<tr class="srow".*?</tr>', table, re.S)
    titles = [unescape(re.search(r'class="ptitle"[^>]*>(.*?)</(?:a|span)>', row).group(1)) for row in rows]
    assert titles == [*[f"Song {i}" for i in range(8)], "Offline song"]
    assert table.count('class="pl-thumb"') == 8
    assert [unescape(re.search(r'class="pl-play" href="([^"]+)"', row).group(1)) for row in rows[:8]] == [
        f"https://music.youtube.com/watch?v=v{9-i}&list=quiet-list" for i in range(8)]
    assert 'class="pl-play"' not in rows[-1]
    assert 'class="ui-arrow' not in section
    assert "redis-more" not in section


def test_home_renders_for_you_and_no_sync_elsewhere(store):
    # Complete cards are available both inline and through the fragment endpoint.
    iid = store.accounts.upsert_account("main", None, True)
    day = 86400.0
    now = 200 * day
    wheelhouse = [
        {'key': 'gem|x', 'video_id': 'v1', 'title': 'Gem', 'artist': 'X', 'genre': 'house'},
        *({'key': f'song {i}|artist {i}', 'video_id': f'v{i}', 'title': f'Song {i}',
           'artist': f'Artist {i}', 'genre': 'house'} for i in range(2, 6)),
    ]
    seed_publication_subjects(store, wheelhouse)
    store.history.record_history_plays(iid, now - 120 * day, ["gem|x"], source_timezone="UTC")
    store.history.record_history_plays(iid, now - 110 * day, ["gem|x"], source_timezone="UTC")
    store.settings.set_setting("last_sync_at", str(now - day))
    returning_listener(store, 'mixes', now=now)
    publish_bundles(store, {'all': {'wheelhouse': wheelhouse}}, now, retain_payload=False)
    c = _client(store, now=now, prepare=False)

    cards = c.get("/home/cards").text
    assert "More in your wheelhouse" in cards    # the wheelhouse card heading
    assert "Gem" in cards                        # the track appears in the card row
    assert 'data-key="gem|x"' in cards

    home = c.get("/").text
    assert 'data-key="gem|x"' in home
    assert 'class="home-status card card--status is-live global-now-playing"' in home

    # Now Playing belongs to the shared layout, not Home, so it follows the user across the app.
    playlists = c.get("/playlists")
    assert playlists.status_code == 200
    assert "global-now-playing" in playlists.text
    assert "global-now-playing" in c.get("/charts").text


def test_steering_panel_hidden_until_genres_exist(store):
    """A library genre summary can expose tuning while the mixes are still preparing."""
    from yt_playlist.rec import taste_analysis

    iid = store.accounts.upsert_account("main", None, True)
    returning_listener(store, 'mixes', now=1000)
    c = _client(store, prepare=False)
    response = c.get("/home/feed")
    assert response.status_code == 200 and 'id="home-feed"' in response.text
    assert 'id="fingerprint"' not in response.text

    t = store.tracks.upsert_track("v1", "Song", "Band", None, None)
    store.tracks.set_track_genre(t, "Techno")
    store.history.record_history_plays(iid, 1.0, ["song|band"], source_timezone="UTC")
    taste_analysis.prepare_library_summary(store, 1000)
    response = c.get("/home/feed")
    assert response.status_code == 200
    assert 'id="fingerprint"' in response.text
    assert 'id="home-feed"' in response.text


def test_home_page_has_steering_and_fingerprint(store):
    iid = store.accounts.upsert_account("main", None, True)
    t = store.tracks.upsert_track("v1", "Song", "Band", None, None)
    store.tracks.set_track_genre(t, "Techno")
    store.tracks.set_track_year(t, "1999")
    store.history.record_history_plays(iid, 1.0, ["song|band"], source_timezone="UTC")
    store.settings.set_setting("last_sync_at", "1000")            # synced user -> feed (with fingerprint/steer) renders
    store.settings.set_setting("onboard_dismissed", "1")          # graduated user: testing the normal feed, not onboarding
    _publish_existing_listener_mix(store)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    response = c.get("/")
    assert response.status_code == 200
    html = response.text
    assert 'id="fingerprint"' in html
    assert 'aria-label="Tune these mixes"' in html
    assert 'aria-controls="fp-dialog"' in html
    assert 'id="fp-genre-bars"' not in html
    assert '/home/steer' not in html
    dialog = c.get('/home/fingerprint?dialog=true')
    assert dialog.status_code == 200
    assert 'id="fp-genre-bars"' in dialog.text
    assert 'type="range"' in dialog.text
    assert '/home/steer' in dialog.text
    assert '/home/breadth' in dialog.text
    assert '/taste' in html


def test_home_steer_writes_lean_not_permanent_weight(store):
    iid = store.accounts.upsert_account("main", None, True)
    _publish_existing_listener_mix(store)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/home/steer", data={"axis": "genre:techno", "weight": "1.5"})
    assert r.status_code == 200
    assert 'id="home-mode-cards"' in r.text
    assert 'id="home-feed"' not in r.text
    assert "genre:techno" not in store.rec_model.get_weights()      # NOT a permanent write anymore
    # permanent is neutral (1.0) so effective target 1.5 -> lean 1.5
    assert store.rec_model.get_lean("genre:techno") == 1.5


def test_presync_shows_recs_placeholder_not_feed(store):
    """The festival guides arrival until useful offers exist, across a full sync."""
    iid = store.accounts.upsert_account("main", None, True)
    store.tracks.upsert_track("v1", "Gem", "X", None, None)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")

    html = c.get("/").text
    assert 'id="arrival-title"' in html
    assert 'Welcome to TuneConsole' in html
    assert 'class="festival-map"' in html
    assert store.settings.get_setting('home_format') is None
    assert "More in your wheelhouse" not in html                           # feed hidden pre-sync

    store.settings.set_setting("last_sync_at", "1000")
    assert 'id="onboarding-feed"' in c.get("/").text
    assert 'aria-labelledby="setup-details-title"' in c.get("/").text


def test_enrich_nudge_is_gone(store):
    """The manual-enrichment nag was removed (auto-enrich worker handles it now): no #enrich-nudge,
    and the old dismiss endpoint no longer exists."""
    c = _client(store)
    store.settings.set_setting("last_sync_at", "1000")             # synced user, the old nudge's trigger
    assert "enrich-nudge" not in c.get("/").text
    assert c.post("/onboard/enrich/dismiss").status_code == 404   # route removed entirely


def test_fingerprint_bar_shows_effective_multiplier(store):
    iid = store.accounts.upsert_account("main", None, True)
    t = store.tracks.upsert_track("v1", "Song", "Band", None, None)
    store.tracks.set_track_genre(t, "Techno")
    store.history.record_history_plays(iid, 1.0, ["song|band"], source_timezone="UTC")
    store.settings.set_setting("last_sync_at", "1000")
    store.settings.set_setting("onboard_dismissed", "1")       # graduated user: testing the normal feed, not onboarding
    fam = __import__("yt_playlist.util.genre_map", fromlist=["family"]).family("Techno")
    store.rec_model.set_lean(f"genre:{fam}", 1.5, 1000.0)        # standing lean, permanent still 1.0
    _publish_existing_listener_mix(store)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    dialog = c.get('/home/fingerprint?dialog=true')
    assert dialog.status_code == 200
    assert f'"axis":"genre:{fam}"' in dialog.text
    assert 'value="1.5"' in dialog.text


def test_taste_fingerprint_marks_transient(store):
    # Recent heavy listening to one family -> a live transient marker on that bar, distinct from the
    # held-steer thumb (which stays at `effective`). The two mood signals no longer share one position.
    from yt_playlist.rec import taste_analysis
    from yt_playlist.util.matching import identity_key
    iid = store.accounts.upsert_account("main", None, True)
    keys = [identity_key(f'song{i}', 'band') for i in range(10)]
    seed_publication_subjects(store, [{'key': key, 'video_id': f'v{i}', 'title': f'song{i}',
        'artist': 'band', 'genre': 'Techno'} for i, key in enumerate(keys)])
    store.history.record_history_plays(iid, 1000.0, keys, source_timezone="UTC")
    store.settings.set_setting("last_sync_at", "1000")              # fresh -> staleness 1.0
    fam = __import__("yt_playlist.util.genre_map", fromlist=["family"]).family("Techno")
    fp = taste_analysis.taste_fingerprint(store, 1000.0)
    entry = next(e for e in fp["families"] if e["name"] == fam)
    assert "live" in entry and "live_active" in entry
    assert entry["live_active"] is True                    # recent heavy plays are a live signal
    assert entry["live"] >= entry["effective"]             # a positive play lean pushes the marker up
    assert 0.0 <= entry["live"] <= 2.0


def test_taste_fingerprint_marker_inert_when_stale(store):
    # A stale sync relaxes the transient to ~0, so no bar claims a live marker (families still present).
    from yt_playlist.rec import taste_analysis
    from yt_playlist.util.matching import identity_key
    iid = store.accounts.upsert_account("main", None, True)
    keys = [identity_key(f'song{i}', 'band') for i in range(5)]
    seed_publication_subjects(store, [{'key': key, 'video_id': f'v{i}', 'title': f'song{i}',
        'artist': 'band', 'genre': 'Techno'} for i, key in enumerate(keys)])
    store.history.record_history_plays(iid, 1.0, keys, source_timezone="UTC")
    store.settings.set_setting("last_sync_at", "1.0")
    now = 1.0 + 100 * 86400                                  # 100 days later -> deeply stale
    fp = taste_analysis.taste_fingerprint(store, now)
    assert fp["families"], "expected played families present regardless of staleness"
    for e in fp["families"] + fp["eras"]:
        assert e["live_active"] is False


def test_fingerprint_subgenres_attached_and_deduped(store):
    """taste_fingerprint attaches a family's drill-down subgenres (eager, toggled client-side): the
    family token itself is excluded (no self-duplicate), and a subgenre with a lean is promoted to its
    own top bar instead of appearing under the family."""
    from yt_playlist.rec import taste_analysis
    iid = store.accounts.upsert_account("main", None, True)
    t = store.tracks.upsert_track("v1", "Song", "Band", None, None)
    store.tracks.set_track_genre(t, "techno")
    store.history.record_history_plays(iid, 1.0, ["song|band"], source_timezone="UTC")
    fp = taste_analysis.taste_fingerprint(store, 1000.0)
    techno = next(f for f in fp["families"] if f["name"] == "techno")
    subnames = [s["name"] for s in techno["subgenres"]]
    assert "minimal techno" in subnames          # a real subgenre is offered
    assert "techno" not in subnames              # the family token is not duplicated as its own subgenre
    # pin minimal techno -> it leaves the drill-down (promoted to a top bar)
    store.rec_model.set_lean("genre:minimal techno", 1.2, 1.0)
    fp2 = taste_analysis.taste_fingerprint(store, 1000.0)
    techno2 = next(f for f in fp2["families"] if f["name"] == "techno")
    assert "minimal techno" not in [s["name"] for s in techno2["subgenres"]]
    assert "minimal techno" in [f["name"] for f in fp2["families"]]   # now a top bar


def test_fingerprint_add_reaches_zero_play_niche(store):
    """POST /home/fingerprint/add axis='genre:gqom' -> 200; returns just the re-rendered genre bars
    (for swapping #fp-genre-bars, leaving the picker alive) with a gqom bar, and persists the lean."""
    c = _client(store, prepare=False)
    _publish_existing_listener_mix(store)
    r = c.post("/home/fingerprint/add", data={"axis": "genre:gqom"})
    assert r.status_code == 200
    # The added axis renders as a steerable bar in the returned bars partial...
    assert 'genre:gqom' in r.text and 'type="range"' in r.text
    # ...and the response is JUST the bars partial, not the whole feed (so the picker isn't swapped).
    assert 'gen-lanes' not in r.text and 'fp-genre-pick' not in r.text
    # The lean was persisted
    assert store.rec_model.get_lean("genre:gqom") == 1.0


def test_fingerprint_renders_pinned_genre_beyond_top6():
    """Regression: a pinned axis (explicit lean) past the top-6 must still render as a bar. Two ways it
    lands past index 6: (a) a zero-play niche appended at the end; (b) a low-share PLAYED family the
    user added (e.g. 'rock-post', the family post-rock lives under) -- this one has share>0, so the old
    `share == 0.0` filter dropped it ('select rock-post, nothing happens'). Both must show now."""
    from jinja2 import Environment, FileSystemLoader
    from yt_playlist.web.icons import arrow_icon
    env = Environment(loader=FileSystemLoader("src/yt_playlist/web/templates"))
    env.globals['arrow_icon'] = arrow_icon
    families = [{"name": f"fam{i}", "share": 1.0 - i * 0.1, "weight": 1.0, "effective": 1.0,
                 "pinned": False} for i in range(7)]                 # 7 played, un-pinned families
    # (b) a low-share PLAYED family the user pinned, ranked past the top 6:
    families.append({"name": "rock-post", "share": 0.02, "weight": 1.0, "effective": 1.0, "pinned": True})
    # (a) a zero-play niche appended at the end:
    families.append({"name": "psytrance", "share": 0.0, "weight": 1.0, "effective": 1.0, "pinned": True})
    fp = {"families": families, "eras": [], "breadth": 0.5, "breadth_bias": 0.0}
    html = env.get_template("_partials/taste_fingerprint.html").render(fingerprint=fp)
    assert "genre:rock-post" in html      # low-share pinned PLAYED family renders (the reported bug)
    assert "genre:psytrance" in html      # pinned zero-play niche still renders
    assert "genre:fam6" not in html       # an UN-pinned family past the top-6 stays hidden (compact)


def test_home_breadth_bar_is_interactive(store):
    """#7: the Breadth bar steers the feed. It posts to /home/breadth bound to the breadth_bias param,
    and a post persists the bias and re-renders only the four-card board."""
    from yt_playlist.rec import rec_params
    iid = store.accounts.upsert_account("main", None, True)
    t = store.tracks.upsert_track("v1", "Song", "Band", None, None)
    store.tracks.set_track_genre(t, "Techno")
    store.history.record_history_plays(iid, 1.0, ["song|band"], source_timezone="UTC")
    c = _client(store, prepare=False)
    _publish_existing_listener_mix(store)
    feed = c.get("/")
    assert feed.status_code == 200 and 'aria-controls="fp-dialog"' in feed.text
    assert 'name="breadth_bias"' not in feed.text
    dialog = c.get('/home/fingerprint?dialog=true')
    assert dialog.status_code == 200
    assert 'hx-post="/home/breadth"' in dialog.text
    assert 'name="breadth_bias"' in dialog.text
    r = c.post("/home/breadth", data={"breadth_bias": "0.5"})
    assert r.status_code == 200 and 'id="home-mode-cards"' in r.text
    assert r.text.count('class="gen-card card"') == 4
    assert 'id="home-feed"' not in r.text
    assert rec_params.get_param(store, "breadth_bias") == 0.5
    c.post("/home/breadth", data={"breadth_bias": "9"})
    assert rec_params.get_param(store, "breadth_bias") == 1.0


def test_home_has_no_stance_toggle(store):
    """#7: the noisy wheelhouse/explore toggle is gone (route, container, and its unique label)."""
    iid = store.accounts.upsert_account("main", None, True)
    t = store.tracks.upsert_track("v1", "Song", "Band", None, None)
    store.tracks.set_track_genre(t, "Techno")
    store.history.record_history_plays(iid, 1.0, ["song|band"], source_timezone="UTC")
    store.settings.set_setting("last_sync_at", "1000")
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    html = c.get("/").text
    assert "/home/stance" not in html
    assert "fp-stance" not in html
    assert "Try something new" not in html               # the toggle's unique label
    assert c.post("/home/stance", data={"stance": "explore"}).status_code == 404


def test_home_genres_taxonomy(store):
    """GET /home/genres -> 200, the full taxonomy (families + sub-genres) the Home genre picker filters
    client-side. Must include a known subgenre ('minimal techno') so a zero-play genre is pinnable."""
    c = _client(store, prepare=False)
    _publish_existing_listener_mix(store)
    r = c.get("/home/genres")
    assert r.status_code == 200
    opts = r.json()["options"]
    names = {o["name"] for o in opts}
    kinds = {o["kind"] for o in opts}
    assert "minimal techno" in names           # a known subgenre is searchable
    assert kinds == {"family", "genre"}         # both kinds are tagged for the dropdown
