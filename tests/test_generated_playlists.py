"""Generated proto-playlists: the two dated, saveable lanes on Home.

Covers generated-playlist creation and learning provenance. Generated sources
do not supply track-level taste evidence until explicit adoption; membership
must never suppress a track that remains eligible for recommendations.
"""
import json
from tests.enrichment_support import seed_audio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from fastapi.testclient import TestClient

from yt_playlist.util.matching import identity_key
from yt_playlist.web.app import create_app
from yt_playlist.library import playlist_generated
from tests.conftest import FakeClient
from tests.playlist_fixture_support import remote_catalog
from tests.home_fixture_support import home_routes_only, publish_menu, without_metadata_providers
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, mock_snapshot_coordinator, native_sql_calls)


def _client(store, provider):
    return TestClient(create_app(store, provider, now_fn=lambda: 1.0), base_url="http://127.0.0.1")


def _seed_generated(store, iid, n=3):
    """A generated-group playlist of n tracks (unplayed). Returns (pid, ytm, track identity_keys)."""
    pid = store.playlists.upsert_playlist(iid, "PLG", "Gen - June 21 2026", n, "h", 1.0)
    tids = [store.tracks.upsert_track(f"g{i}", f"G{i}", "GenArt", None, None, 1) for i in range(n)]
    store.playlists.set_playlist_tracks(pid, tids)
    store.playlists.set_playlist_group("PLG", "Generated")
    store.playlists.mark_generated_playlist('PLG')
    return pid, "PLG", {identity_key(f"G{i}", "GenArt") for i in range(n)}


def test_generated_excluded_until_promoted(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    pid, ytm, gkeys = _seed_generated(store, iid, n=2)
    dao = store.rec_eligibility

    assert dao.excluded_playlist_ids() == {pid}                 # quarantined while "Generated"

    # Playing it does NOT graduate it. Adoption is an explicit act, not a side effect of listening.
    for _ in range(5):
        store.history.record_history_plays(iid, 1.0, list(gkeys), source_timezone="UTC")
    assert dao.excluded_playlist_ids() == {pid}                 # still quarantined despite heavy plays

    store.playlists.set_playlist_group(ytm, "Faves")
    assert dao.excluded_playlist_ids() == {pid}                # organization never adopts
    store.playlists.adopt_generated_playlist(ytm)
    assert dao.excluded_playlist_ids() == set()

    store.playlists.set_playlist_group(ytm, "Generated")                  # back into quarantine
    assert dao.excluded_playlist_ids() == set()                 # moving groups cannot revoke adoption


def test_generated_tracks_in_no_basket_until_promoted(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    _pid, ytm, gkeys = _seed_generated(store, iid, n=3)
    dao = store.rec_evidence

    for _ in range(2):
        store.history.record_history_plays(iid, 1.0, list(gkeys), source_timezone="UTC")
    keys = {k for b in dao.rec_baskets() for k in b}
    assert not (keys & gkeys)                                   # ...generated songs pollute no basket

    store.playlists.set_playlist_group(ytm, "Faves")                     # promote it into the collection
    store.playlists.adopt_generated_playlist(ytm)
    keys2 = {k for b in dao.rec_baskets() for k in b}
    assert gkeys & keys2                                        # ...so they rejoin the signal


def test_generate_endpoint_creates_and_groups(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    fc = FakeClient()
    c = _client(store, lambda: {iid: fc})
    tracks = json.dumps([{"video_id": "v1", "title": "S1", "artist": "A", "album": "", "thumbnail": ""},
                         {"video_id": "v2", "title": "S2", "artist": "A", "album": "",
                          "thumbnail": "https://example.com/second-song.jpg"}])
    fc._catalog.update(remote_catalog(json.loads(tracks)))

    r = c.post("/home/generate", data={"name": "More in your wheelhouse - June 21 2026", "tracks": tracks})

    assert r.status_code == 200
    assert fc.created and fc.created[0][1] == "More in your wheelhouse - June 21 2026"
    assert fc.added and fc.added[0][1] == ["v1", "v2"]
    new_ytm = fc.created[0][0]
    assert store.playlists.get_playlist_groups().get(new_ytm) == "Generated"   # auto-grouped
    # optimistically materialized so it shows in the Playlists tab right away (no sync needed)
    saved = next(p for p in store.playlists.get_playlists() if p.provider_playlist_id == new_ytm)
    assert f'data-generated-playlist="{new_ytm}"' in r.text
    assert f'window.location.assign("/playlist/{saved.id}")' in r.text
    assert saved.title == "More in your wheelhouse - June 21 2026" and saved.track_count == 2
    assert saved.created_at == 1.0
    assert saved.thumbnail == "https://example.com/second-song.jpg"


def test_generate_result_play_and_redirect(store):
    """Save & play opens the normal playlist without an intermediate success screen."""
    iid = store.accounts.upsert_account("main", "cred", None, True)
    fc = FakeClient()
    c = _client(store, lambda: {iid: fc})
    tracks = json.dumps([{"video_id": "v1", "title": "S1", "artist": "A", "album": "", "thumbnail": ""}])
    fc._catalog.update(remote_catalog(json.loads(tracks)))

    r = c.post("/home/generate", data={"name": "Mix - June 21 2026", "tracks": tracks})

    assert r.status_code == 200
    assert 'data-generated-playlist=' in r.text
    assert 'gen-done' not in r.text and 'status-label--success' not in r.text
    assert 'Generated</strong> group' not in r.text
    assert 'window.location.assign("/playlist/' in r.text      # this tab goes to the playlist page
    assert "genComplete(" in r.text                          # close the request's loading window


@pytest.mark.parametrize("recipe", [{"model": "fresh"},
                                    {"model": "mode", "lane": "fresh", "mode_id": 1}])
def test_fresh_creation_starts_enrichment_and_page_rejoins(store, monkeypatch, recipe):
    from yt_playlist.providers import musicbrainz
    from tests.conftest import only_provider

    iid = store.accounts.upsert_account("main", "cred", None, True)
    only_provider(store, "musicbrainz")
    started, release = threading.Event(), threading.Event()

    def enrich(title, artist, *, observations=None):
        started.set()
        assert release.wait(timeout=10)
        return "Rock", "1998", None, [], []

    monkeypatch.setattr(musicbrainz, "enrich_full", enrich)
    with _client(store, lambda: {iid: FakeClient(catalog=remote_catalog([
            {'video_id': 'fresh1', 'title': 'New Song', 'artist': 'Artist'}]))}) as c:
        try:
            response = c.post("/home/generate", data={
                "name": "Fresh songs", "recipe": json.dumps(recipe),
                "tracks": json.dumps([{"video_id": "fresh1", "title": "New Song", "artist": "Artist"}])})

            assert response.status_code == 200
            assert started.wait(timeout=2)  # No visit to the playlist page or manual enrich click needed.
            playlist = store.playlists.get_playlists()[0]
            assert f'data-generated-playlist="{playlist.provider_playlist_id}"' in response.text
            assert f'window.location.assign("/playlist/{playlist.id}")' in response.text
            job = c.app.state.ctx.jobs.find_active(playlist.id)
            assert job is not None
            page = c.get(f"/playlist/{playlist.id}").text
            assert f"enrichPanel({playlist.id}, {job.id}, 'waterfall'" in page
            assert c.post(f"/playlist/{playlist.id}/enrich").json()["job_id"] == job.id
        finally:
            release.set()

        events = c.get(f"/playlist/enrich/events/{job.id}").text
        delivered = [json.loads(line[6:]) for line in events.splitlines() if line.startswith('data: ')]
        assert any(event.get('row_html') for event in delivered)
        assert delivered[-1] == {'type': 'end', 'error': False}
        track = store.charts.playlist_tracks_detail(playlist.id)[0]
        assert track["genre"] == "Rock" and track["year"] == "1998"


@pytest.mark.parametrize('recipe', [{'model': 'fresh'},
                                  {'model': 'mode', 'lane': 'fresh', 'mode_id': 1}])
def test_fresh_acceptance_drains_enrichment_on_early_failure(store, monkeypatch, recipe):
    from yt_playlist.providers import waterfall

    entered, release, draining = threading.Event(), threading.Event(), threading.Event()
    contexts = []
    make_client, run_waterfall = _client, waterfall.run_waterfall

    def held_waterfall(*args, **kwargs):
        entered.set()
        assert release.wait(30), 'Controlled enrichment was not released'
        return run_waterfall(*args, **kwargs)

    def observed_client(*args, **kwargs):
        client = make_client(*args, **kwargs)
        ctx = client.app.state.ctx
        contexts.append(ctx)
        post, wait = client.post, ctx.work.wait

        def fail_after_generation(url, *args, **kwargs):
            response = post(url, *args, **kwargs)
            if url == '/home/generate':
                assert response.status_code == 200 and entered.wait(5)
                raise AssertionError('Controlled assertion after generation')
            return response

        def observed_wait(timeout):
            draining.set()
            return wait(timeout)

        monkeypatch.setattr(client, 'post', fail_after_generation)
        monkeypatch.setattr(ctx.work, 'wait', observed_wait)
        return client

    monkeypatch.setattr(waterfall, 'run_waterfall', held_waterfall)
    monkeypatch.setattr(f'{__name__}._client', observed_client)
    with ThreadPoolExecutor(max_workers=1) as executor:
        acceptance = executor.submit(test_fresh_creation_starts_enrichment_and_page_rejoins,
                                     store, monkeypatch, recipe)
        try:
            assert entered.wait(10)
            assert draining.wait(10), 'The original test did not enter application shutdown'
            assert not acceptance.done()
            assert 'run' in contexts[0].work.active
        finally:
            release.set()
        with pytest.raises(AssertionError, match='Controlled assertion after generation'):
            acceptance.result(timeout=20)
    assert not contexts[0].work.active
    store.settings.set_setting('after generated app shutdown', 'usable')
    assert store.settings.get_setting('after generated app shutdown') == 'usable'


def test_non_fresh_creation_does_not_start_playlist_enrichment(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    c = _client(store, lambda: {iid: FakeClient(catalog=remote_catalog([
        {'video_id': 'v1', 'title': 'Song', 'artist': 'Artist'}]))})
    response = c.post("/home/generate", data={
        "name": "Wheelhouse", "recipe": json.dumps({"model": "mode", "lane": "wheelhouse"}),
        "tracks": json.dumps([{"video_id": "v1", "title": "Song", "artist": "Artist"}])})
    assert response.status_code == 200
    playlist = store.playlists.get_playlists()[0]
    assert f'data-generated-playlist="{playlist.provider_playlist_id}"' in response.text
    assert f'window.location.assign("/playlist/{playlist.id}")' in response.text
    assert not c.app.state.ctx.jobs.has_active()


def test_save_button_opens_generating_screen(store):
    """The Save & play button opens the cool generating screen during the click (a user gesture), so
    the browser does not block it."""
    iid = store.accounts.upsert_account("main", "cred", None, True)
    t = store.tracks.upsert_track("v1", "Song", "Artist", "Alb", None, 1)
    pl = store.playlists.upsert_playlist(iid, "PL", "P", 1, "h", 1.0)
    store.playlists.set_playlist_tracks(pl, [t])
    store.history.record_history_plays(iid, 1.0, [identity_key("Song", "Artist")], source_timezone="UTC")
    store.settings.set_setting("last_sync_at", "1.0")        # synced -> the rec feed (generated cards) renders
    c = _client(store, lambda: {iid: FakeClient()})

    r = c.get("/")
    app_js = c.get("/static/playlist-generation.js")

    assert r.status_code == 200 and app_js.status_code == 200
    assert "function genOpenYT(" in app_js.text
    assert "window.open('', '_blank')" in app_js.text       # opens during the user gesture
    assert 'renderMixLaunch(tab.document, record.context)' in app_js.text


def test_generated_membership_is_retained_without_suppressing_candidates(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    t = [store.tracks.upsert_track(f"v{i}", f"S{i}", "Art", None, None, 1) for i in range(2)]
    gen = store.playlists.upsert_playlist(iid, "PLG", "From your catalog - June 21 2026", 2, "h", 1.0)
    store.playlists.set_playlist_tracks(gen, t)
    store.playlists.set_playlist_group("PLG", "Generated")
    store.playlists.mark_generated_playlist('PLG')

    keys = store.rec_eligibility.generated_track_keys()
    assert keys == {identity_key("S0", "Art"), identity_key("S1", "Art")}
    assert not store.rec_eligibility.suppressed_candidate_keys(keys, 1)
    assert not any(keys & set(basket) for basket in store.rec_evidence.rec_baskets())


def test_generate_endpoint_rejects_empty(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post("/home/generate", data={"name": "x", "tracks": "[]"})
    assert r.status_code == 200 and "Couldn't save" in r.text


@pytest.fixture
def generated_card_store(tmp_path, monkeypatch, native_sql_calls):
    # Rendering consumes completed candidates, independently of genre import
    # and snapshot coordination. Keep the actual card, mode and evidence files.
    with bounded_consumer_schema((('house', 'House'),), snapshot_guards=False) as schema:
        with _file_consumer_store(tmp_path, schema, snapshot_guards=False) as database:
            with mock_snapshot_coordinator(database, monkeypatch):
                yield database


def test_home_renders_generated_cards(
        generated_card_store, unlocked_home_controls, home_routes_only, without_metadata_providers):
    store = generated_card_store
    # Cards are lazy-loaded via /home/cards; assert against that endpoint instead of /.
    import numpy as np
    iid = store.accounts.upsert_account("main", "cred", None, True)
    store.settings.set_setting("last_sync_at", "1.0")        # synced -> the rec feed (generated cards) renders
    store.modes.replace_modes([
        {"mode_id": 1, "label": "a", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 80, "rep_keys": []},
    ], retired_ids=[], now=1.0)
    publish_menu(store, 1, count=2, mode_ids=(1, 1),
                 labels=('Comfort listening', 'More in your wheelhouse'))
    with _client(store, lambda: {iid: FakeClient()}) as c:
        r = c.get("/home/cards")
        assert r.status_code == 200
        assert "More in your wheelhouse" in r.text and "Play this mix" in r.text


def test_comfort_proto_card_dj_ordered_with_genre(store, monkeypatch):
    """A proto-card must be ordered BEFORE it's shown: no back-to-back same artist, and genres
    attached so a genre journey is possible. Reproduces the 'comfort playlist clustered by artist,
    no genre journey' bug. The DJ used to run only at save, on data that had already dropped genre.
    Uses shuffle journey so the within-band artist-spacing guarantee applies to all items together."""
    from yt_playlist.rec import home_cards as home
    from yt_playlist.rec import recipes
    from yt_playlist.rec.surfaces import ForYouItem
    store.accounts.upsert_account("main", "cred", None, True)
    spec = ([("Hermanos", "Latin")] * 4 + [("Younger", "Electronica")] * 3
            + [("Ritmo", "Electronica")] * 2 + [("Supertramp", "Rock")])   # arrives artist-clustered
    items = []
    for i, (art, genre) in enumerate(spec):
        tid = store.tracks.upsert_track(f"v{i}", f"S{i}", art, "", None, 1)
        store.tracks.set_track_genre(tid, genre)
        items.append(ForYouItem(f"S{i}", art, "", f"v{i}", None, 5, "most-played",
                                identity_key(f"S{i}", art), "comfort"))
    monkeypatch.setattr(recipes, "roll_recipe",
                        lambda *a, **k: {"facets": {}, "journey": "shuffle", "dj": {"seed": 1}})

    card = home._carded(store, "comfort", "Comfort listening", items, now=1.0)

    out = card["tracks"]
    assert len(out) == len(items)                                   # all rows survive the ordering
    adj_same = sum(1 for a, b in zip(out, out[1:]) if a.artist == b.artist)
    assert adj_same == 0                                            # artists spaced in the PREVIEW
    assert all(getattr(t, "genre", "") for t in out)               # genres attached -> journey possible


def test_create_generated_playlist_stores_recipe_and_versions(store):
    from yt_playlist.library import executor, playlist_undo
    iid = store.accounts.upsert_account("main", "cred", None, True)
    fc = FakeClient()
    tracks = [{"video_id": f"v{i}", "title": f"S{i}", "artist": "A" if i % 2 else "B"} for i in range(4)]
    fc._catalog.update(remote_catalog(tracks))
    recipe = {"model": "fresh", "facets": {"genres": ["house"], "eras": ["2010"]},
              "dj": {"stickiness": 0.0, "seed": 5}}
    r1 = playlist_generated.create_generated_playlist(store, "Fresh songs - June 21 2026", list(tracks),
                                            fc, now=1.0, account_id=iid, recipe=recipe)
    assert r1["title"] == "Fresh songs - June 21 2026 #1"          # versioned at save
    assert store.playlist_recipes.get_recipe(r1["new_ytm"])["facets"]["genres"] == ["house"]   # recipe stored
    r2 = playlist_generated.create_generated_playlist(store, "Fresh songs - June 21 2026", list(tracks),
                                            fc, now=2.0, account_id=iid, recipe=recipe)
    assert r2["title"] == "Fresh songs - June 21 2026 #2"          # next version that day


# --- garbage collection of unplayed generated playlists (daily worker) -------------------------

def _seed_generated_at(store, iid, created, n=3, ytm="PLG"):
    """A Generated-group playlist of n unplayed tracks whose first_seen (creation) is `created`."""
    pid = store.playlists.upsert_playlist(iid, ytm, "Gen mix", n, "h", created)
    tids = [store.tracks.upsert_track(f"{ytm}{i}", f"{ytm}T{i}", "GenArt", None, None, 1) for i in range(n)]
    store.playlists.set_playlist_tracks(pid, tids)
    store.playlists.set_playlist_group(ytm, "Generated")
    store.playlists.mark_generated_playlist(ytm)
    return pid, ytm, {identity_key(f"{ytm}T{i}", "GenArt") for i in range(n)}


def test_gc_deletes_stale_unplayed_generated(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library import executor, playlist_undo
    from yt_playlist.util.action_kinds import is_undoable
    iid = store.accounts.upsert_account("main", "cred", None, True)
    day = 86400.0; now = 100 * day
    pid, ytm, _ = _seed_generated_at(store, iid, created=now - 10 * day)   # 10d old, never played
    fc = FakeClient()

    collected = playlist_generated.gc_generated_playlists(store, {iid: fc}, now)     # 7-day grace by default

    assert [c["ytm"] for c in collected] == [ytm]
    assert store.playlists.get_playlist(pid) is None         # pruned locally
    assert fc.deleted == [ytm]                      # deleted on YouTube
    act = store.actions.get_actions()[0]
    assert act.kind == "gc_generated" and is_undoable(act.kind)


def test_gc_keeps_young_generated(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library import executor, playlist_undo
    iid = store.accounts.upsert_account("main", "cred", None, True)
    day = 86400.0; now = 100 * day
    pid, _ytm, _ = _seed_generated_at(store, iid, created=now - 2 * day)   # still inside the grace window
    fc = FakeClient()

    assert playlist_generated.gc_generated_playlists(store, {iid: fc}, now) == []
    assert store.playlists.get_playlist(pid) is not None and fc.deleted == []


def test_gc_keeps_generated_played_since_creation(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library import executor, playlist_undo
    iid = store.accounts.upsert_account("main", "cred", None, True)
    day = 86400.0; now = 100 * day
    pid, _ytm, keys = _seed_generated_at(store, iid, created=now - 10 * day)
    store.history.record_history_plays(iid, now - day, list(keys), source_timezone="UTC")

    assert playlist_generated.gc_generated_playlists(store, {iid: FakeClient()}, now) == []
    assert store.playlists.get_playlist(pid) is not None


def test_gc_keeps_generated_when_at_least_half_played(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library import executor, playlist_undo
    iid = store.accounts.upsert_account("main", "cred", None, True)
    day = 86400.0; now = 100 * day
    pid, _ytm, _ = _seed_generated_at(store, iid, created=now - 10 * day, n=4)
    half = [identity_key(f"PLGT{i}", "GenArt") for i in (0, 1)]            # 2 of 4 = 50% played since creation
    store.history.record_history_plays(iid, now - day, half, source_timezone="UTC")

    assert playlist_generated.gc_generated_playlists(store, {iid: FakeClient()}, now) == []
    assert store.playlists.get_playlist(pid) is not None


def test_gc_counts_live_generated_playback_without_feeding_organic_history(store):
    iid = store.accounts.upsert_account('main', 'cred', None, True)
    day, now = 86400.0, 100 * 86400.0
    pid, ytm, _ = _seed_generated_at(store, iid, created=now - 10 * day, n=4)
    for i in (0, 1):
        store.history.record_play_event(iid, identity_key(f'PLGT{i}', 'GenArt'), f'PLG{i}',
                                        now - day, playlist_ytm_id=ytm)
    assert all(at is None for at in store.charts.get_playlist_track_recency()[pid])
    assert playlist_generated.gc_generated_playlists(store, {iid: FakeClient()}, now) == []
    assert store.playlists.get_playlist(pid) is not None


def test_gc_deletes_generated_when_under_half_played(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library import executor, playlist_undo
    iid = store.accounts.upsert_account("main", "cred", None, True)
    day = 86400.0; now = 100 * day
    _pid, ytm, _ = _seed_generated_at(store, iid, created=now - 10 * day, n=4)
    store.history.record_history_plays(iid, now - day, [identity_key("PLGT0", "GenArt")], source_timezone="UTC")
    fc = FakeClient()

    collected = playlist_generated.gc_generated_playlists(store, {iid: fc}, now)
    assert [c["ytm"] for c in collected] == [ytm]                          # a stray play or two doesn't save it


def test_gc_ignores_plays_before_creation(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library import executor, playlist_undo
    iid = store.accounts.upsert_account("main", "cred", None, True)
    day = 86400.0; now = 100 * day
    _pid, ytm, keys = _seed_generated_at(store, iid, created=now - 10 * day)
    store.history.record_history_plays(iid, now - 20 * day, list(keys), source_timezone="UTC")
    fc = FakeClient()

    collected = playlist_generated.gc_generated_playlists(store, {iid: fc}, now)
    assert [c["ytm"] for c in collected] == [ytm]                          # a pre-creation play doesn't save it


def test_gc_ignores_non_generated_playlists(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library import executor, playlist_undo
    iid = store.accounts.upsert_account("main", "cred", None, True)
    day = 86400.0; now = 100 * day
    t = store.tracks.upsert_track("v1", "S", "A", None, None, 1)
    pl = store.playlists.upsert_playlist(iid, "PLN", "Normal", 1, "h", now - 30 * day)   # old + unplayed, but real
    store.playlists.set_playlist_tracks(pl, [t])
    fc = FakeClient()

    assert playlist_generated.gc_generated_playlists(store, {iid: fc}, now) == []
    assert store.playlists.get_playlist(pl) is not None and fc.deleted == []


def test_gc_deletion_is_undoable(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library import executor, playlist_undo
    iid = store.accounts.upsert_account("main", "cred", None, True)
    day = 86400.0; now = 100 * day
    _pid, ytm, _ = _seed_generated_at(store, iid, created=now - 10 * day)
    fc = FakeClient()
    playlist_generated.gc_generated_playlists(store, {iid: fc}, now)
    aid = store.actions.get_actions()[0].id

    playlist_undo.undo_action(store, aid, {iid: fc}, now + day)
    assert fc.created and fc.created[0][1] == "Gen mix"     # recreated from backup
    assert store.actions.get_action(aid).status == "undone"


def test_carded_orders_by_recipe_journey(store, monkeypatch):
    """_carded must order the proto by the recipe's journey. Force 'warm_up' and assert the preview
    rises in energy thirds (and still spaces artists / carries genre)."""
    from yt_playlist.rec import home_cards as home
    from tests.musical_support import prepare_profiles
    from yt_playlist.rec import musical_character, recipes
    from yt_playlist.rec.surfaces import ForYouItem
    store.accounts.upsert_account("main", "cred", None, True)
    # mellow vs intense families, interleaved so source order is NOT already sorted.
    spec = [("ambient", "Calm"), ("metal", "Loud"), ("classical", "Calm"), ("punk", "Loud"),
            ("folk", "Calm"), ("dnb", "Loud"), ("jazz", "Calm"), ("trance", "Loud"),
            ("blues", "Calm"), ("techno", "Loud"), ("ambient", "Calm"), ("metal", "Loud")]
    items = []
    for i, (genre, artist) in enumerate(spec):
        tid = store.tracks.upsert_track(f"v{i}", f"S{i}", artist, "", None, 1)
        store.tracks.set_track_genre(tid, genre)
        seed_audio(store, tid, energy=0 if artist == "Calm" else 1)
        items.append(ForYouItem(f"S{i}", artist, "", f"v{i}", None, 1, "r",
                                identity_key(f"S{i}", artist), "comfort"))
    prepare_profiles(store)
    monkeypatch.setattr(recipes, "roll_recipe",
                        lambda *a, **k: {"facets": {}, "journey": "warm_up", "dj": {"seed": 1}})

    card = home._carded(store, "comfort", "Comfort listening", list(items), now=1.0)

    out = card["tracks"]
    assert card["recipe"]["journey"] == "warm_up"
    profiles = musical_character.load(store, [t.key for t in out])
    e = [musical_character.intensity({'musical': profiles[t.key]}) for t in out]
    thirds = [sum(e[:4]) / 4, sum(e[4:8]) / 4, sum(e[8:]) / 4]
    assert thirds[0] < thirds[1] < thirds[2]               # warm_up rises across ALL thirds (shuffle ~17%)
    assert all(getattr(t, "genre", "") for t in out)        # genres still attached
