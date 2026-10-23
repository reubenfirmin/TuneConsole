"""Prepared offers remain disposable; exposure and current eligibility remain authoritative."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import logging
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import numpy as np
import pytest

from tests.conftest import FakeClient
from yt_playlist.core.activity import Activity
from yt_playlist.core.store import Store
from yt_playlist.rec import home_cards, home_prepared, home_themes, mix_tuning, mode_surfaces, surfaces
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.util.matching import identity_key
from yt_playlist.web.app import create_app


def seed(store):
    modes, bundles = [], {"all": {}, "_meta": {"comfort_pool": 100, "full_pools": True}}
    for mid, genre in enumerate(("Jazz", "Techno", "Classical", "Ambient", "Trance", "Indie Rock")):
        modes.append({"mode_id": mid, "label": genre, "families": [], "size": 50,
                      "centroid": np.eye(6)[mid], "rep_keys": []})
        bundles[str(mid)] = {}
        for lane in ("wheelhouse", "explore", "comfort", "fresh"):
            items = []
            for i in range(8):
                title, artist, video = f"{lane} {mid} {i}", f"Artist {mid} {i}", f"v-{lane}-{mid}-{i}"
                key = identity_key(title, artist)
                if lane == "fresh":
                    store.discovery.upsert_discovered_track(key, video, title, artist, "", None, genre,
                                                            None, None, 1)
                else:
                    tid = store.tracks.upsert_track(video, title, artist, "", None)
                    store.tracks.set_track_genre(tid, genre)
                items.append({"key": key, "title": title, "artist": artist, "album": "", "video_id": video,
                              "thumbnail": None, "genre": genre, "plays": 0, "reason": "", "lane": lane})
            bundles[str(mid)][lane] = items
            bundles["all"].setdefault(lane, []).extend(items)
    store.modes.replace_modes(modes, [], 1)
    store.rec_surface.put_proposals("mode_bundles", bundles, 1)
    return bundles


@pytest.fixture
def prepared(store):
    seed(store)
    result = home_prepared.prepare(store, 1000)
    assert result is not None
    return result


def service(store):
    return home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__))


def test_generation_shares_play_counts_and_facets_across_summaries(store, monkeypatch):
    seed(store)
    repo = type(store.rec_query)
    original = repo.play_counts
    calls = []

    def counted(self):
        calls.append(True)
        return original(self)

    def forbidden(*args, **kwargs):
        raise AssertionError('Home must reuse the generation summaries')

    monkeypatch.setattr(repo, 'play_counts', counted)
    monkeypatch.setattr(repo, 'genre_play_distribution', forbidden)
    monkeypatch.setattr(repo, 'era_play_distribution', forbidden)
    assert home_prepared.prepare(store, 1000) is not None
    assert len(calls) == 1


def test_preparation_is_bounded_complete_and_records_no_exposure(store, prepared):
    assert len(prepared["menus"]) == home_prepared.MAX_MENUS
    assert all(menu["protos"] for menu in prepared["menus"])
    assert {p["mode_id"] for m in prepared["menus"] for p in m["protos"] if p.get("mode_id") is not None} == set(range(6))
    assert store.modes.impression_counts() == {}
    assert store.conn.execute("SELECT COUNT(*) FROM rec_served_impressions").fetchone()[0] == 0
    assert all(t["offered_count"] == 0 for t in store.discovery.get_discovered_tracks())
    assert all(p["recipe"]["experiment_id"] == p["experiment_id"]
               for menu in prepared["menus"] for p in menu["protos"])


def test_missing_candidate_dependencies_keep_last_usable_home(store, prepared):
    with store.conn:
        store.conn.execute("DELETE FROM rec_proposals WHERE surface IN ('mode_bundles','fresh_pool')")
    assert home_prepared.prepare(store, 1001) is None
    assert store.rec_surface.get_proposals(home_prepared.SURFACE) == prepared
    assert home_prepared.cards(store, prepared, 1001, service(store), record=False).protos


def test_initial_import_can_offer_catalog_mix_before_tags_or_models(store, monkeypatch):
    entry_ids = []
    for i in range(24):
        entry_ids.append(store.tracks.upsert_track(f'import-{i}', f'Song {i}', f'Artist {i}', None, None))
    calculate = mode_surfaces._calculate_bundles

    def enrich_during_calculation(reader, now, policies):
        result = calculate(reader, now, policies)
        store.enrichment.apply_provider_fields(entry_ids[0], 'musicbrainz',
            {'genre': 'House'}, providers=['musicbrainz'])
        return result

    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', enrich_during_calculation)
    bundles = mode_surfaces.prepare_bundles(store, 1000)
    assert bundles and bundles['all']['explore']
    assert not store.modes.list_modes(active_only=True)
    assert not store.rec_model.rec_vectors_count()

    def forbidden(*args, **kwargs):
        raise AssertionError('Home assembly and serving must only consume prepared rankings')

    monkeypatch.setattr(surfaces, 'explore_for_you', forbidden)
    monkeypatch.setattr(surfaces, 'for_you', forbidden)
    prepared = home_prepared.prepare(store, 1001)
    assert home_prepared._valid(prepared)
    result = home_prepared.cards(store, prepared, 1001, service(store), record=False)
    catalog = next(p for p in result.protos if p['lane'] == 'explore')
    assert len(catalog['tracks']) >= 4
    assert catalog.get('mode_id') is None
    disliked = catalog['tracks'][0]['key']
    store.rec_model.record_dislike(disliked, until=None, now=1002)
    result = home_prepared.cards(store, prepared, 1002, service(store), record=False)
    assert all(t['key'] != disliked for p in result.protos for t in p['tracks'])


def test_changed_artist_links_allow_preparation_but_current_canonical_mutes_still_apply(store, monkeypatch):
    seed(store)
    store.rec_model.record_feedback('for_you', 'artist:Guest', 'mute', now=999)
    original = home_cards.HomeCards.cards
    corrected = []

    def correct_credit(self, request):
        result = original(self, request)
        if not corrected:
            proto = next(p for p in result.protos if p['lane'] != 'fresh')
            track = proto['tracks'][0]
            corrected.append(deepcopy(proto))
            store.tracks.upsert_track('imported', 'Imported song', 'New artist', '', None)
            # The display credit stays unchanged; the corrected canonical artist
            # relationship must still apply the existing mute at serving time.
            store.tracks.upsert_track(track['video_id'], track['title'], track['artist'], '', None,
                                      credits=[{'name': 'Guest'}])
        return result

    monkeypatch.setattr(home_cards.HomeCards, 'cards', correct_credit)
    prepared = home_prepared.prepare(store, 1000)
    assert prepared is not None
    key = corrected[0]['tracks'][0]['key']
    assert key in {t['key'] for m in prepared['menus'] for p in m['protos'] for t in p['tracks']}
    current = home_prepared._eligible_protos(store, corrected, 1001)
    assert current and current[0]['tracks']
    assert key not in {t['key'] for p in current for t in p['tracks']}


def test_ordinary_home_reads_use_prepared_results_without_selection_or_analytics(store, prepared, monkeypatch):
    # Force the description/audit fallback on actual selected tracks. A fully
    # tagged fixture used to miss its whole-library artist lookup.
    for menu in prepared['menus']:
        for proto in menu['protos']:
            proto['tracks'][0]['genre'] = ''
    store.rec_surface.put_proposals(home_prepared.SURFACE, prepared, 1000)
    def forbidden(*a, **kw):
        raise AssertionError("Heavy Home work must stay in background preparation")
    for target, name in ((mode_surfaces, "assemble_cards"), (home_themes, "genre_options"),
                         (surfaces, "for_you"), (surfaces, "rediscover_playlists"),
                         (surfaces, "rediscover_albums")):
        monkeypatch.setattr(target, name, forbidden)
    for name in ("play_counts", "track_audio_features", "genre_play_distribution", "era_play_distribution",
                 "artist_genre_years"):
        monkeypatch.setattr(store.rec_query, name, forbidden)
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.settings.set_setting("last_sync_at", "1000")
    store.settings.set_setting("onboard_dismissed", "1")
    client = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1001),
                        base_url="http://127.0.0.1")
    for path in ("/", "/home/cards", "/home/feed", "/home/discovery", "/home/genres"):
        response = client.get(path)
        assert response.status_code == 200
        assert "Finding four ways into your library" not in response.text
        if path in ("/", "/home/cards", "/home/feed"):
            assert 'class="gen-card' in response.text or 'class="card gen-card' in response.text
    assert 'hx-trigger="load" hx-swap="innerHTML" class="card-row-loading"' not in client.get("/").text
    assert client.post("/home/refresh-cards").status_code == 200
    assert client.post("/home/refresh-card/wheelhouse").status_code == 200


def test_genre_preference_edits_schedule_and_publish_a_new_prepared_generation(store, prepared):
    scheduled = []
    store.rec_model.set_weight('genre:techno', 0.5, now=1000)
    retained = home_prepared.load(store, 1000, lambda: scheduled.append(True))
    assert retained['tuning'] == prepared['tuning']
    assert scheduled == [True]
    refreshed = home_prepared.prepare(store, 1000)
    assert refreshed is not None
    assert refreshed['tuning'] != prepared['tuning']
    assert home_prepared.load(store, 1000)['tuning'] == refreshed['tuning']
    store.rec_model.reset_weights()
    assert home_prepared.tuning_key(store, 1000) == prepared['tuning']


@pytest.mark.parametrize('damage', ['experiment_id', 'rediscover'])
def test_corrupt_prepared_payload_never_breaks_home_routes(store, prepared, damage):
    payload = deepcopy(prepared)
    if damage == 'experiment_id':
        payload['menus'][0]['protos'][0].pop('experiment_id')
    else:
        payload['rediscover'] = None
    store.rec_surface.put_proposals(home_prepared.SURFACE, payload, 1000)
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    store.settings.set_setting('last_sync_at', '1000')
    store.settings.set_setting('onboard_dismissed', '1')
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1001)
    scheduled = []
    app.state.ctx.rec_worker.trigger_home_refresh = lambda **kw: scheduled.append(True)
    client = TestClient(app, base_url='http://127.0.0.1')
    for path in ('/', '/home/cards', '/home/feed', '/home/discovery', '/home/genres'):
        assert client.get(path).status_code == 200
    assert scheduled


def test_saved_mix_is_immediately_excluded_from_prepared_owned_cards(store, prepared, monkeypatch):
    initial = home_prepared.cards(store, prepared, 1000, service(store), record=False)
    proto = next(p for p in initial.protos if p['lane'] in ('wheelhouse', 'explore', 'comfort'))
    track = proto['tracks'][0]
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    pid = store.playlists.upsert_playlist(iid, 'saved-mix', 'Saved mix', 1, None, 1001)
    tid = store.tracks.track_ids_for_videos([track['video_id']])[track['video_id']]
    store.playlists.set_playlist_tracks(pid, [tid])
    store.playlists.mark_generated_playlist('saved-mix')

    def forbidden(*args, **kwargs):
        raise AssertionError('Serving a prepared offer must not rank the catalog')

    monkeypatch.setattr(mode_surfaces, 'assemble_cards', forbidden)
    monkeypatch.setattr(surfaces, 'for_you', forbidden)
    current = home_prepared.cards(store, prepared, 1002, service(store), record=False)
    assert all(t['key'] != track['key'] for p in current.protos
               for t in [*p['tracks'], *(p.get('counterfactual') or {}).get('tracks', [])])
    # The retained payload is reusable; serving only filters detached copies.
    assert any(t['key'] == track['key'] for m in prepared['menus'] for p in m['protos'] for t in p['tracks'])


def test_cached_playlist_uses_current_title_membership_and_eligibility(store, prepared):
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    pid = store.playlists.upsert_playlist(iid, 'old-list', 'Old title', 5, None, 1000)
    # The prepared teaser deliberately names tracks no longer in the playlist.
    prepared['rediscover'] = [{'id': pid, 'title': 'Old title', 'track_count': 5,
                               'tracks': [{'title': 'Removed teaser'}]}]
    tid = store.tracks.upsert_track('current', 'Current teaser', 'Current artist', None, None)
    store.playlists.set_playlist_tracks(pid, [tid])
    store.playlists.upsert_playlist(iid, 'old-list', 'New title', 1, 'new', 1001)
    current = home_prepared.collections(store, prepared, 1001)['rediscover']
    assert current[0]['title'] == 'New title' and current[0]['track_count'] == 1
    assert [t['title'] for t in current[0]['tracks']] == ['Current teaser']
    store.playlists.set_playlist_tracks(pid, [])
    assert home_prepared.collections(store, prepared, 1001)['rediscover'] == []
    with store.conn:
        store.conn.execute('UPDATE playlists SET title=?,track_count=0 WHERE id=?', ('Empty now', pid))
    assert home_prepared.collections(store, prepared, 1001)['rediscover'] == []


def test_previews_and_repeated_fragments_do_not_inflate_exposure(store, prepared):
    home_prepared.cards(store, prepared, 1000, service(store), record=False)
    assert store.modes.impression_counts() == {}
    home_prepared.cards(store, prepared, 1000, service(store))
    counts = {t["identity_key"]: t["offered_count"] for t in store.discovery.get_discovered_tracks()}
    impressions = store.conn.execute("SELECT COUNT(*) FROM rec_served_impressions").fetchone()[0]
    home_prepared.cards(store, prepared, 1001, service(store))
    assert {t["identity_key"]: t["offered_count"] for t in store.discovery.get_discovered_tracks()} == counts
    assert store.conn.execute("SELECT COUNT(*) FROM rec_served_impressions").fetchone()[0] == impressions


def test_hold_window_keeps_the_choice_through_refill_and_new_evidence(store, prepared):
    first = home_prepared.cards(store, prepared, 1000, service(store))
    ids = {p["experiment_id"] for p in first.protos}
    replacement = deepcopy(prepared)
    for mode in replacement["modes"]:
        mode["size"] = 100000 if mode["mode_id"] not in {p.get("mode_id") for p in first.protos} else 1
    for epoch in range(20, 40):
        store.modes.log_impressions(epoch, [(str(mid), mid) for mid in range(6)], 1001)
    held = home_prepared.cards(store, replacement, 1002, service(store))
    assert {p["experiment_id"] for p in held.protos} == ids


def test_hidden_presync_and_onboarding_menus_are_not_exposures(store, prepared):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    client = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000),
                        base_url="http://127.0.0.1")
    assert client.get("/").status_code == 200
    assert store.modes.impression_counts() == {}
    store.modes.replace_modes([], list(range(6)), 1000)
    store.settings.set_setting("last_sync_at", "1000")
    assert client.get("/").status_code == 200
    assert store.modes.impression_counts() == {}


@pytest.mark.parametrize("bad", [None, {}, {"schema": 999}, "bad-json"])
def test_missing_or_corrupt_cache_queues_preparation_without_ranking(store, monkeypatch, bad):
    monkeypatch.setattr(store.rec_surface, "get_proposals", lambda key: bad if key == home_prepared.SURFACE else None)
    calls = []
    assert home_prepared.load(store, 1000, lambda: calls.append(True)) is None
    result = home_prepared.cards(store, None, 1000, service(store), lambda: calls.append(True))
    assert result.protos == []
    assert calls


def test_malformed_menu_is_discarded_and_damaged_tuned_cache_keeps_baseline(store, prepared, monkeypatch):
    corrupt = deepcopy(prepared)
    corrupt["menus"][0]["protos"] = [None]
    assert not home_prepared._valid(corrupt)
    original = store.rec_surface.get_proposals
    def get(surface):
        if surface == "home_tuned":
            raise ValueError("corrupt JSON")
        return original(surface)
    monkeypatch.setattr(store.rec_surface, "get_proposals", get)
    assert home_prepared.load(store, 1001) == prepared


def test_current_exclusions_and_removed_sources_apply_to_cached_mixes(store, prepared):
    initial = home_prepared.cards(store, prepared, 1000, service(store), record=False)
    assert initial.protos
    key = initial.protos[0]["tracks"][0]["key"]
    store.rec_model.record_dislike(key, until=None, now=1001)
    other = initial.protos[0]["tracks"][1]["key"]
    with store.conn:
        store.conn.execute("DELETE FROM library_memberships WHERE entry_id IN "
                           "(SELECT e.id FROM catalog_entries e JOIN recordings r ON r.id=e.recording_id "
                           "WHERE r.identity_key=?)", (other,))
    result = home_prepared.cards(store, prepared, 1001, service(store), record=False)
    shown = {t["key"] for p in result.protos for t in p["tracks"]}
    assert key not in shown and other not in shown
    assert result.protos


def test_cached_discovery_honors_unavailability_and_current_tile_metadata(store, prepared):
    initial = home_prepared.cards(store, prepared, 1001, service(store), record=False)
    fresh = next(p for p in initial.protos if p["lane"] == "fresh")
    key = fresh["tracks"][0]["key"]
    with store.conn:
        store.conn.execute("UPDATE youtube_items SET available=0 WHERE recording_id IN "
                           "(SELECT id FROM recordings WHERE identity_key=?)", (key,))
    cards = home_prepared.cards(store, prepared, 1001, service(store), record=False)
    assert key not in {t["key"] for p in cards.protos for t in p["tracks"]}
    store.discovery.upsert_discovered_album("new-album", "Artist", "Album", "2020", None, 1000, genre="Jazz")
    tiles = home_prepared.prepare(store, 1001)
    assert tiles["discovery_albums"]
    with store.conn:
        store.conn.execute("UPDATE albums SET genre='Techno' WHERE id IN "
                           "(SELECT album_id FROM album_provider_ids "
                           "WHERE provider='youtube' AND provider_id='new-album')")
    store.rec_model.set_weight("genre:techno", 0, lo=0, now=1002)
    assert home_prepared.discovery(store, tiles, 1002)["discovery_albums"] == []


def test_feedback_changes_the_next_choice_and_tracks_can_repeat(store, prepared):
    first = home_prepared.cards(store, prepared, 1000, service(store), record=False)
    first_modes = {p["mode_id"] for p in first.protos if p.get("mode_id") is not None}
    # Strong exposure without picks is evidence against repeating the same themes.
    for epoch in range(20, 70):
        store.modes.log_impressions(epoch, [(str(mid), mid) for mid in first_modes], 500)
    second = home_prepared.cards(store, prepared, 1001, service(store), record=False)
    second_modes = {p["mode_id"] for p in second.protos if p.get("mode_id") is not None}
    assert second_modes - first_modes
    a = {t["key"] for p in first.protos for t in p["tracks"]}
    b = {t["key"] for p in second.protos for t in p["tracks"]}
    assert a & b  # Reuse of constituent tracks remains valid.


def test_cache_rebuild_and_restart_preserve_evidence_and_experiment_identity(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    store.init_schema()
    seed(store)
    before = home_prepared.prepare(store, 1000)
    offer = home_prepared.cards(store, before, 1000, service(store))
    stats = store.modes.impression_counts()
    ids = {p["experiment_id"] for p in offer.protos}
    assert stats and home_prepared.prepare(store, 1001)
    assert store.modes.impression_counts() == stats
    store.conn.close()
    reopened = Store(path)
    reopened.init_schema()
    try:
        after = home_prepared.load(reopened, 1002)
        assert after and reopened.modes.impression_counts() == stats
        assert ids <= {p["experiment_id"] for m in after["menus"] for p in m["protos"]}
    finally:
        reopened.conn.close()


def test_generation_race_retains_previous_cache(store, prepared, monkeypatch):
    original = home_themes.genre_options
    def changed(reader, **kwargs):
        result = original(reader, **kwargs)
        store.rec_model.record_dislike("x", until=None, now=1002)
        return result
    monkeypatch.setattr(home_themes, "genre_options", changed)
    assert home_prepared.prepare(store, 1002) is None
    assert store.rec_surface.get_proposals(home_prepared.SURFACE) == prepared


def test_setup_sql_obeys_total_budget_and_preserves_previous_cache(store, prepared, monkeypatch):
    def slow(reader, **kwargs):
        return reader.conn.execute('WITH RECURSIVE slow(n) AS (VALUES(0) UNION ALL '
            'SELECT n+1 FROM slow WHERE n<1000000000) SELECT sum(n) FROM slow').fetchone()

    monkeypatch.setattr(home_prepared, 'PREPARE_BUDGET_S', 0.2)
    monkeypatch.setattr(home_themes, 'genre_options', slow)
    started = time.monotonic()
    assert home_prepared.prepare(store, 1002) is None
    assert time.monotonic() - started < 1.5
    assert store.rec_surface.get_proposals(home_prepared.SURFACE) == prepared


def test_small_preparation_batches_accumulate_alternatives_across_attempts(store, prepared, monkeypatch):
    clock = [100]
    original = home_cards.HomeCards.cards

    def one_menu(self, request):
        result = original(self, request)
        clock[0] += 6
        return result

    store.rec_surface.put_proposals(home_prepared.SURFACE, None, 1000)
    monkeypatch.setattr(home_prepared.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(home_prepared, 'PREPARE_BUDGET_S', 10)
    monkeypatch.setattr(home_cards.HomeCards, 'cards', one_menu)
    for size in range(1, home_prepared.MAX_MENUS + 1):
        payload = home_prepared.prepare(store, 1000 + size)
        assert payload is not None
        assert len(payload['menus']) == size
    assert {p['mode_id'] for m in payload['menus'] for p in m['protos']
            if p.get('mode_id') is not None} == set(range(6))
    assert store.modes.impression_counts() == {}


def test_preparation_can_publish_while_ordinary_unrelated_ui_writes_continue(store, prepared, monkeypatch):
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    original = home_themes.genre_options

    def with_ui_writes(reader, **kwargs):
        options = original(reader, **kwargs)
        # These writes continue while source catch-up is pending. Neither changes
        # the consumed model generation or the selected candidates' eligibility.
        store.settings.set_setting('last_sync_at', '1002')
        store.settings.set_setting('home_tab', 'mixes')
        return options

    monkeypatch.setattr(home_themes, 'genre_options', with_ui_writes)
    assert home_prepared.prepare(store, 1002) is not None
    assert store.rec_refresh.has_pending_changes()  # publication did not wait for the source backlog to drain


def test_automatic_refreshes_share_backoff_and_retry_without_another_request(store, monkeypatch):
    from yt_playlist.rec import rec_worker

    now = [10]
    monkeypatch.setattr(rec_worker.time, 'monotonic', lambda: now[0])
    worker = RecWorker(SimpleNamespace(store=store, work=Activity(), logger=logging.getLogger(__name__),
                                       now_fn=lambda: 1000), debounce_s=0)
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    monkeypatch.setattr(home_prepared, 'prepare', lambda *a: None)
    monkeypatch.setattr(mode_surfaces, 'prepare_bundles', lambda *a: {})
    worker.trigger_home_refresh()
    worker._run_pending()
    assert worker.jobs.retry['home'].dirty and not worker.jobs.has('home')
    for _ in range(16):
        now[0] += 1
        worker._refresh_bundles()
        worker._retry_home()
        assert not worker.jobs.has('home')
    now[0] = 40
    worker._retry_home()
    assert worker.jobs.has('home')
    worker.shutdown()


def test_tuned_result_is_reused_and_expiry_restores_baseline(store, prepared):
    with mix_tuning.adjusting(store, 1000):
        store.rec_model.set_lean("genre:jazz", 2, 1000)
    result = service(store).cards(home_cards.CardsRequest(1000, record=False))
    home_prepared.remember(store, prepared, result, 1000)
    tuned = home_prepared.load(store, 1001)
    assert tuned["tuning"] == home_prepared.tuning_key(store)
    assert len(tuned["menus"]) == 1
    assert home_prepared.load(store, 1000 + mix_tuning.TTL_S)["tuning"] == prepared["tuning"]
    assert store.rec_model.get_leans() == {}


def test_cold_requests_coalesce_home_work_without_requesting_a_model_fit(store, monkeypatch):
    worker = RecWorker(SimpleNamespace(store=store, work=Activity(), logger=logging.getLogger(__name__),
                                       now_fn=lambda: 1000), debounce_s=0)
    launches = []
    monkeypatch.setattr(worker, "_start_loop", lambda: launches.append(True))
    for _ in range(10):
        worker.trigger_home_refresh()
    assert launches == [True] and worker.jobs.has('home')
    assert not worker.jobs.has('full') and not worker.jobs.has('modes')
    worker.shutdown()


def test_ui_remains_available_during_real_preparation_sql_and_shutdown_interrupts(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    store.init_schema()
    seed(store)
    started = threading.Event()
    def slow(reader, **kwargs):
        assert reader.conn is not store.conn
        reader.conn.create_function("started", 0, lambda: (started.set(), 0)[1])
        reader.conn.execute("WITH RECURSIVE slow(n) AS (VALUES(started()) UNION ALL SELECT n+1 "
                            "FROM slow WHERE n<1000000000) SELECT sum(n) FROM slow").fetchone()
    monkeypatch.setattr(home_themes, "genre_options", slow)
    worker = RecWorker(SimpleNamespace(store=store, work=Activity(), logger=logging.getLogger(__name__),
                                       now_fn=lambda: 1000), debounce_s=0)
    def interact():
        release = store.priority.enter()
        try:
            store.settings.set_setting("ui", "responsive")
            return store.settings.get_setting("ui")
        finally:
            release()
    try:
        worker.trigger_home_refresh()
        assert started.wait(5)
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(interact).result(2) == "responsive"
        worker.shutdown()
        worker.join(3)
    finally:
        worker.shutdown()
        worker.join(5)
        store.conn.close()
