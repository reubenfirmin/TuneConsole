"""Prepared offers remain disposable; exposure and current eligibility remain authoritative."""
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
import json
import logging
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import numpy as np
import pytest

from tests.conftest import FakeClient
from tests.musical_support import prepare_vibes
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
    store.rec_proposals.put_proposals("mode_bundles", bundles, 1)
    return prepare_vibes(store)


@pytest.fixture
def prepared(store):
    seed(store)
    result = home_prepared.prepare(store, 1000)
    assert result is not None
    return result


def service(store):
    return home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__))


def test_history_diagnostics_do_not_change_generation_but_effective_allocation_does(store):
    from yt_playlist.rec import choice_bandit, group_serving
    groups = [{'group_id': gid, 'version': gid + '-v', 'parent_version': None, 'size': 20}
              for gid in ('a', 'b')]
    store.rec_proposals.put_proposals(group_serving.SURFACE, {'generation': 'g', 'groups': groups}, 90)
    stats = {'generation': 'g', 'parameters': choice_bandit.parameters(store), 'at': 90,
             'expires_at': 200, 'state_version': 'first',
             'rows': [{'group_id': 'a', 'estimate': .8}, {'group_id': 'b', 'estimate': .2}]}
    store.rec_proposals.put_proposals('group_choice_bandit', stats, 90)
    original = home_prepared._proposal_generation(store, None, 100)
    refreshed = {**stats, 'at': 101, 'offered': 99, 'state_version': 'new-diagnostics',
                 'rows': [{**row, 'offered': 99, 'menus': ['unanswered']} for row in stats['rows']]}
    store.rec_proposals.put_proposals('group_choice_bandit', refreshed, 101)
    assert home_prepared._proposal_generation(store, None, 102) == original
    changed = {**refreshed, 'rows': [{'group_id': 'a', 'estimate': .2}, {'group_id': 'b', 'estimate': .8}]}
    store.rec_proposals.put_proposals('group_choice_bandit', changed, 103)
    assert home_prepared._proposal_generation(store, None, 104) != original
    # Expiry and future inputs still invalidate a learned allocation.
    expired = home_prepared._proposal_generation(store, None, 201)
    assert expired != home_prepared._proposal_generation(store, None, 104)
    store.rec_proposals.put_proposals('group_choice_bandit', {**stats, 'at': 300}, 300)
    assert home_prepared._proposal_generation(store, None, 201) == expired
    # Pool publication and changed reward configuration remain real inputs.
    store.rec_proposals.put_proposals('group_choice_bandit', stats, 90)
    store.rec_proposals.put_proposals('fresh_pool', {'items': [{'key': 'new-candidate'}]}, 105)
    changed_pool = home_prepared._proposal_generation(store, None, 106)
    assert changed_pool != original
    store.rec_proposals.put_proposals('group_choice_bandit', {
        **stats, 'parameters': {**stats['parameters'], 'window_d': -1}}, 105)
    assert home_prepared._proposal_generation(store, None, 106) != changed_pool


def test_generation_shares_play_counts_and_facets_across_summaries(store, monkeypatch):
    seed(store)
    repo = type(store.rec_query)
    original = repo.play_counts
    calls = []
    proposal_repo = type(store.rec_proposals)
    read_proposals = proposal_repo.get_proposals
    bundle_reads = []

    def bounded_bundle_read(self, surface):
        if surface == 'mode_bundles':
            bundle_reads.append(self)
            assert bundle_reads.count(self) == 1, 'Each snapshot must decode its candidate pool only once'
        return read_proposals(self, surface)

    def counted(self):
        calls.append(True)
        return original(self)

    def forbidden(*args, **kwargs):
        raise AssertionError('Home must reuse the generation summaries')

    monkeypatch.setattr(repo, 'play_counts', counted)
    monkeypatch.setattr(proposal_repo, 'get_proposals', bounded_bundle_read)
    monkeypatch.setattr(repo, 'genre_play_distribution', forbidden)
    monkeypatch.setattr(repo, 'era_play_distribution', forbidden)
    assert home_prepared.prepare(store, 1000) is not None
    assert len(calls) == 1
    assert len(bundle_reads) == 2  # Independent discovery and mix snapshots.


def test_album_budget_deferral_still_publishes_mixes_with_last_good_album_context(store, monkeypatch):
    from yt_playlist.core.activity import WorkBudgetExceeded
    from yt_playlist.rec import album_choices
    from yt_playlist.rec.worker_tasks import ServingTasks
    seed(store)
    albums = {'items': [{'browse_id': 'last-good', 'title': 'Retained album'}],
              'context': {'model': 'original album prediction'}}
    store.rec_proposals.put_proposals(album_choices.SURFACE, albums, 999)

    def exhausted(*args):
        raise WorkBudgetExceeded('Album preparation exceeded its independent budget')

    monkeypatch.setattr(album_choices, 'prepare', exhausted)
    tasks = ServingTasks(SimpleNamespace(store=store, now_fn=lambda: 1000, client_provider=lambda: {},
        logger=logging.getLogger(__name__)), lambda name, target, *args, **kwargs: target(*args, **kwargs))
    assert tasks.home()  # Albums request another attempt through the existing Home backoff.
    result = store.rec_proposals.get_proposals(home_prepared.SURFACE)
    assert result['menus'] and result['album_choices'] == albums['items']
    assert result['album_choice_context'] == albums['context']
    assert store.rec_proposals.get_proposals(album_choices.SURFACE) == albums


def test_budget_retry_reuses_completed_genre_setup_and_publishes_a_menu(store, monkeypatch):
    from yt_playlist.core.activity import WorkBudgetExceeded
    seed(store)
    original_options, original_cards = home_themes.genre_options, home_cards.HomeCards.cards
    options_calls, cards_calls = [], []

    def options(reader, **kwargs):
        options_calls.append(True)
        return original_options(reader, **kwargs)

    def cards(self, request):
        cards_calls.append(True)
        if len(cards_calls) == 1:
            raise WorkBudgetExceeded('The first menu starts after this batch deadline')
        return original_cards(self, request)

    monkeypatch.setattr(home_themes, 'genre_options', options)
    monkeypatch.setattr(home_cards.HomeCards, 'cards', cards)
    assert home_prepared.prepare(store, 1000) is None
    result = home_prepared.prepare(store, 1001)
    assert result and any(menu['protos'] for menu in result['menus'])
    assert len(options_calls) == 1


def test_slow_revisit_does_not_starve_first_mix_during_source_updates(store, monkeypatch):
    seed(store)
    from yt_playlist.core.activity import WorkBudgetExceeded
    calls = []

    def delayed(reader, *args, **kwargs):
        calls.append(True)
        raise WorkBudgetExceeded('Large actual-history summary reached its separate deadline')

    monkeypatch.setattr(surfaces, 'rediscover_tracks', delayed)
    result = home_prepared.prepare(store, 1000)
    assert result and any(menu['protos'] for menu in result['menus'])
    assert calls and result['rediscover_tracks'] == []
    for now in (1061, 1122):
        store.settings.set_setting('last_sync_at', str(now))
        refreshed = home_prepared.prepare(store, now)
        assert refreshed and any(menu['protos'] for menu in refreshed['menus'])
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == refreshed


def test_preparation_is_bounded_complete_and_records_no_exposure(store, prepared):
    assert len(prepared["menus"]) == home_prepared.MAX_MENUS
    assert all(menu["protos"] for menu in prepared["menus"])
    assert {p["mode_id"] for m in prepared["menus"] for p in m["protos"] if p.get("mode_id") is not None} == set(range(6))
    assert store.modes.impression_counts() == {}
    assert store.conn.execute("SELECT COUNT(*) FROM rec_served_impressions").fetchone()[0] == 0
    assert all(t["offered_count"] == 0 for t in store.discovery.get_discovered_tracks())
    assert all(p["recipe"]["experiment_id"] == p["experiment_id"]
               for menu in prepared["menus"] for p in menu["protos"])


def test_served_whole_menu_probability_is_recorded_and_frozen(store, prepared):
    first = home_prepared.cards(store, prepared, 1001, service(store))
    assignment = first.protos[0]["assignment"]
    assert sum(row["probability"] for row in assignment["candidates"]) == pytest.approx(1)
    assert 0 < assignment["probability"] <= 1
    assert assignment["unit"] == "whole_menu"
    again = home_prepared.cards(store, prepared, 1002, service(store))
    assert again.protos[0]["assignment"] == assignment
    assert [p["experiment_id"] for p in again.protos] == [p["experiment_id"] for p in first.protos]
    impressions = store.conn.execute("SELECT provenance FROM rec_served_impressions").fetchall()
    import json
    live = [json.loads(row[0]) for row in impressions if json.loads(row[0])["policy_kind"] == "live"]
    assert live and all(row["propensity"] == assignment["probability"] for row in live)


def test_missing_candidate_dependencies_keep_last_usable_home(store, prepared):
    with store.conn:
        store.conn.execute("DELETE FROM rec_proposals WHERE surface IN ('mode_bundles','fresh_pool')")
    assert home_prepared.prepare(store, 1001) is None
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == prepared
    assert home_prepared.cards(store, prepared, 1001, service(store), record=False).protos


def test_new_skip_filters_an_already_prepared_menu_without_changing_the_heart(store, prepared):
    first = home_prepared.cards(store, prepared, 1001, service(store), record=False)
    track = next(track for proto in first.protos for track in proto['tracks'])
    iid = store.identities.upsert_identity('Main', 'credential', None, True)
    store.rec_model.record_like(track['key'], 1001)
    store.player_events.record_player_event(
        iid, 'next', track['video_id'], 1, 200, '', None, 1002)
    served = home_prepared.cards(store, prepared, 1003, service(store), record=False)
    assert served.protos
    assert track['key'] not in {item['key'] for proto in served.protos for item in proto['tracks']}
    assert track['key'] in store.rec_model.recent_liked_keys()
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == prepared


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
    store.rec_proposals.put_proposals(home_prepared.SURFACE, prepared, 1000)
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
    store.rec_proposals.put_proposals(home_prepared.SURFACE, payload, 1000)
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


def test_saving_all_wheelhouse_tracks_does_not_change_the_held_cards(store, prepared, monkeypatch):
    initial = home_prepared.cards(store, prepared, 1000, service(store))
    proto = next(p for p in initial.protos if p['lane'] == 'wheelhouse')
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    pid = store.playlists.upsert_playlist(iid, 'saved-mix', 'Saved mix', len(proto['tracks']), None, 1001)
    tids = store.tracks.track_ids_for_videos([track['video_id'] for track in proto['tracks']])
    store.playlists.set_playlist_tracks(pid, list(tids.values()))
    store.playlists.mark_generated_playlist('saved-mix')

    def forbidden(*args, **kwargs):
        raise AssertionError('Serving a prepared offer must not rank the catalog')

    monkeypatch.setattr(mode_surfaces, 'assemble_cards', forbidden)
    monkeypatch.setattr(surfaces, 'for_you', forbidden)
    current = home_prepared.cards(store, prepared, 1002, service(store))
    assert len(current.protos) == len(current.slots) == 4
    assert {p['lane']: p['tracks'] for p in current.protos} == {
        p['lane']: p['tracks'] for p in initial.protos}
    assert all(not (p.get('assignment') or {}).get('censored') for p in current.protos)
    keys = [t['key'] for t in proto['tracks']]
    assert not store.rec_eligibility.suppressed_candidate_keys(keys, 1002)
    # Actual user feedback must still take effect immediately.
    store.rec_model.record_dislike(keys[0], until=None, now=1003)
    disliked = home_prepared.cards(store, prepared, 1003, service(store))
    assert keys[0] not in {t['key'] for p in disliked.protos for t in p['tracks']}


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
    prepared['choice_context'] = {'at': 999, 'generation': 'original-context'}
    first = home_prepared.cards(store, prepared, 1000, service(store))
    ids = {p["experiment_id"] for p in first.protos}
    replacement = deepcopy(prepared)
    replacement['choice_context'] = {'at': 1001, 'generation': 'later-context'}
    for mode in replacement["modes"]:
        mode["size"] = 100000 if mode["mode_id"] not in {p.get("mode_id") for p in first.protos} else 1
    for epoch in range(20, 40):
        store.modes.log_impressions(epoch, [(str(mid), mid) for mid in range(6)], 1001)
    held = home_prepared.cards(store, replacement, 1002, service(store))
    assert {p["experiment_id"] for p in held.protos} == ids
    # Rebuilding the pool with entirely different menus must not silently reassign
    # the held decision while retaining the old propensity.
    replacement['menus'] = [deepcopy(replacement['menus'][0])]
    for proto in replacement['menus'][0]['protos']:
        proto['experiment_id'] = 'replacement-' + proto['experiment_id']
    after_refill = home_prepared.cards(store, replacement, 1003, service(store))
    assert {p['experiment_id'] for p in after_refill.protos} == ids
    assert after_refill.protos[0]['assignment'] == first.protos[0]['assignment']
    assert after_refill.choice_context == first.choice_context == prepared['choice_context']


def test_hidden_presync_and_onboarding_menus_are_not_exposures(store, prepared):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    client = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000),
                        base_url="http://127.0.0.1")
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="onboarding-feed"' in response.text
    assert 'id="home-feed"' not in response.text
    assert store.modes.impression_counts() == {}
    assert store.rec_ledger.scorecard_inputs(0, 2000)[0] == []
    assert all(t["offered_count"] == 0 for t in store.discovery.get_discovered_tracks())
    store.modes.replace_modes([], list(range(6)), 1000)
    # A usable prepared offer opens Home even without modes. Genuine onboarding
    # requires an unfinished import with no playable prepared mixes.
    with store.conn:
        store.conn.execute("DELETE FROM rec_proposals WHERE surface IN ('home_prepared', 'home_tuned')")
    store.settings.set_setting("last_sync_at", "1000")
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="onboarding-feed"' in response.text
    assert 'id="home-feed"' not in response.text
    assert store.modes.impression_counts() == {}
    assert store.rec_ledger.scorecard_inputs(0, 2000)[0] == []
    assert all(t["offered_count"] == 0 for t in store.discovery.get_discovered_tracks())


def test_visible_prepared_home_without_modes_records_exposures(store, prepared):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.modes.replace_modes([], list(range(6)), 1000)
    store.settings.set_setting("last_sync_at", "1000")
    client = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000),
                        base_url="http://127.0.0.1")

    response = client.get("/")
    assert response.status_code == 200
    assert 'id="home-feed"' in response.text
    assert 'id="onboarding-feed"' not in response.text
    shown = home_prepared.cards(store, prepared, 1000, service(store), record=False).protos
    assert len(shown) == 4
    expected = {(p["lane"], t["key"]) for p in shown for t in p["tracks"]}
    assert all(f'data-key="{key}"' in response.text for _, key in expected)
    assert store.modes.impression_counts() == Counter(p["mode_id"] for p in shown)
    impressions = store.rec_ledger.scorecard_inputs(0, 2000)[0]
    assert len(impressions) == len(expected)
    assert {(r["lane"], r["identity_key"]) for r in impressions} == expected
    offered = {t["identity_key"]: t["offered_count"] for t in store.discovery.get_discovered_tracks()}
    fresh = {key for lane, key in expected if lane == "fresh"}
    assert fresh
    assert offered == {key: int(key in fresh) for key in offered}


@pytest.mark.parametrize("bad", [None, {}, {"schema": 999}, "bad-json"])
def test_missing_or_corrupt_cache_queues_preparation_without_ranking(store, monkeypatch, bad):
    monkeypatch.setattr(store.rec_proposals, "get_proposals", lambda key: bad if key == home_prepared.SURFACE else None)
    calls = []
    assert home_prepared.load(store, 1000, lambda: calls.append(True)) is None
    result = home_prepared.cards(store, None, 1000, service(store), lambda: calls.append(True))
    assert result.protos == []
    assert len(result.slots) == 4 and result.preparing
    assert calls


def test_malformed_menu_is_discarded_and_damaged_tuned_cache_keeps_baseline(store, prepared, monkeypatch):
    corrupt = deepcopy(prepared)
    corrupt["menus"][0]["protos"] = [None]
    assert not home_prepared._valid(corrupt)
    original = store.rec_proposals.get_proposals
    def get(surface):
        if surface == "home_tuned":
            raise ValueError("corrupt JSON")
        return original(surface)
    monkeypatch.setattr(store.rec_proposals, "get_proposals", get)
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


def test_ineligible_held_lane_recovers_from_a_refill_without_changing_other_cards(store, prepared, monkeypatch):
    prepared['menus'] = [prepared['menus'][0]]
    first = home_prepared.cards(store, prepared, 1000, service(store))
    wheelhouse = next(p for p in first.protos if p['lane'] == 'wheelhouse')
    for track in wheelhouse['tracks']:
        store.rec_model.record_dislike(track['key'], until=None, now=1001)
    replacement = deepcopy(prepared)
    replacement['choice_context'] = {'at': 1002, 'generation': 'refilled'}
    offer = next(p for p in replacement['menus'][0]['protos'] if p['lane'] == 'wheelhouse')
    bundles = store.rec_proposals.get_proposals('mode_bundles')
    old_keys = {t['key'] for t in wheelhouse['tracks']}
    offer['tracks'] = [t for t in bundles['all']['wheelhouse'] if t['key'] not in old_keys][:12]
    slot = next(s for s in replacement['menus'][0]['slots'] if s['proto']['lane'] == 'wheelhouse')
    slot['proto'] = offer

    def forbidden(*args, **kwargs):
        raise AssertionError('Recover from prepared cards, without ranking on Home')

    monkeypatch.setattr(home_cards.HomeCards, 'cards', forbidden)
    refreshed = home_prepared.cards(store, replacement, 1002, service(store))
    assert len(refreshed.protos) == len(refreshed.slots) == 4
    assert all(s['proto'] for s in refreshed.slots)
    assert next(p for p in refreshed.protos if p['lane'] == 'wheelhouse')['tracks'] == offer['tracks']
    assert {p['lane']: p['tracks'] for p in refreshed.protos if p['lane'] != 'wheelhouse'} == {
        p['lane']: p['tracks'] for p in first.protos if p['lane'] != 'wheelhouse'}
    assert all(p['assignment']['censored'] for p in refreshed.protos)
    assert refreshed.choice_context == replacement['choice_context']
    again = home_prepared.cards(store, replacement, 1003, service(store))
    assert [(p['lane'], p['tracks']) for p in again.protos] == [(p['lane'], p['tracks']) for p in refreshed.protos]


def test_lost_card_stays_a_clickable_slot_and_polls_for_a_prepared_replacement(store, prepared):
    prepared['menus'] = [prepared['menus'][0]]
    store.rec_proposals.put_proposals(home_prepared.SURFACE, prepared, 1000)
    first = home_prepared.cards(store, prepared, 1000, service(store))
    for track in next(p for p in first.protos if p['lane'] == 'wheelhouse')['tracks']:
        store.rec_model.record_dislike(track['key'], until=None, now=1001)
    calls = []
    result = home_prepared.cards(store, prepared, 1001, service(store), lambda: calls.append(True))
    assert len(result.protos) == 3 and len(result.slots) == 4
    assert result.slots[0]['proto'] is None
    assert result.slots[0]['pool']['lane'] == 'wheelhouse'
    assert result.slots[0]['pool']['parameters'] == {
        'lane': 'wheelhouse', 'epoch': result.epoch, 'include_genres': [], 'avoid_genres': []}
    assert calls
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    client = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1001),
                        base_url='http://127.0.0.1')
    response = client.get('/home/feed')
    assert response.status_code == 200
    assert 'id="gen-wheelhouse"' in response.text
    assert 'hx-post="/home/library-pool"' in response.text
    assert 'hx-get="/home/cards?prepare_attempt=1"' in response.text
    assert 'model keeps learning' not in response.text
    assert 'id="gen-more"' not in response.text


@pytest.mark.parametrize('attempt,next_attempt,interval', [(0, 1, 3), (10, 10, 30), (999, 10, 30), ('invalid', 1, 3)])
@pytest.mark.parametrize('cached_placeholders', [False, True])
def test_startup_cards_keep_polling_with_backoff_until_offers_recover(
        store, prepared, monkeypatch, attempt, next_attempt, interval, cached_placeholders):
    client = TestClient(create_app(store, lambda: {}, now_fn=lambda: 1000,
                                   background_work=False), base_url='http://127.0.0.1')
    pending = None
    if cached_placeholders:
        pending = deepcopy(prepared)
        menu = pending['menus'][0]
        menu['protos'] = []
        menu['slots'] = [{'proto': None, 'pool': service(store).pool_view(
            {'include_genres': [], 'avoid_genres': [], **({'lane': lane, 'epoch': 0} if lane != 'fresh' else {})},
            genre_options=pending['genre_options'])} for lane in ('wheelhouse', 'explore', 'fresh', 'comfort')]
        pending['menus'] = [menu]
        # No other healthy cache exists during this startup. Serving now checks
        # retained surfaces as well as the payload returned by load().
        store.rec_proposals.put_proposals(home_prepared.SURFACE, pending, 1000)
    monkeypatch.setattr(home_prepared, 'load', lambda *args: pending)
    response = client.get(f'/home/cards?prepare_attempt={attempt}')
    assert response.status_code == 200
    assert f'hx-get="/home/cards?prepare_attempt={next_attempt}"' in response.text
    assert f'hx-trigger="every {interval}s"' in response.text
    assert 'hx-post="/home/library-pool"' in response.text

    # A later worker publication terminates polling and replaces the pending
    # slots with playable offers, even after the previous ten-attempt limit.
    store.rec_proposals.put_proposals(home_prepared.SURFACE, prepared, 1000)
    monkeypatch.setattr(home_prepared, 'load', lambda *args: prepared)
    recovered = client.get(f'/home/cards?prepare_attempt={next_attempt}')
    assert recovered.status_code == 200
    assert 'hx-get="/home/cards?prepare_attempt=' not in recovered.text
    assert 'data-key=' in recovered.text
    if cached_placeholders:
        assignment = store.rec_proposals.get_proposals('home_choice')['assignment']
        assert assignment['censored'] and assignment['reason'] == 'prepared_menu_repaired'


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


def test_cache_rebuild_and_restart_preserve_evidence_and_experiment_identity(tmp_path, monkeypatch):
    # This checks durable identity, independently of the separately tested worker deadline.
    monkeypatch.setattr(home_prepared, 'PREPARE_BUDGET_S', 60)
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
    tid = store.tracks.track_ids_for_videos(['v-wheelhouse-0-0'])['v-wheelhouse-0-0']
    store.tracks.set_track_genre(tid, 'House')  # Recompute the completed genre setup for new metadata.
    original = home_themes.genre_options
    def changed(reader, **kwargs):
        result = original(reader, **kwargs)
        store.rec_model.record_dislike("x", until=None, now=1002)
        return result
    monkeypatch.setattr(home_themes, "genre_options", changed)
    assert home_prepared.prepare(store, 1002) is None
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == prepared


def test_setup_sql_obeys_total_budget_and_preserves_previous_cache(store, prepared, monkeypatch):
    tid = store.tracks.track_ids_for_videos(['v-wheelhouse-0-0'])['v-wheelhouse-0-0']
    store.tracks.set_track_genre(tid, 'House')
    def slow(reader, **kwargs):
        return reader.conn.execute('WITH RECURSIVE slow(n) AS (VALUES(0) UNION ALL '
            'SELECT n+1 FROM slow WHERE n<1000000000) SELECT sum(n) FROM slow').fetchone()

    monkeypatch.setattr(home_prepared, 'PREPARE_BUDGET_S', 0.2)
    monkeypatch.setattr(home_themes, 'genre_options', slow)
    started = time.monotonic()
    assert home_prepared.prepare(store, 1002) is None
    assert time.monotonic() - started < 1.5
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == prepared


@pytest.mark.parametrize('boundary', ['before_commit', 'reader_cleanup'])
@pytest.mark.parametrize('reason', ['deadline', 'shutdown'])
def test_publication_acknowledges_commit_across_cleanup_deadline(
        store, prepared, monkeypatch, boundary, reason):
    from yt_playlist.core import activity

    clock, stopping = [100.], [False]
    monkeypatch.setattr(activity, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(home_prepared, 'prepare_discovery', lambda *args: None)
    monkeypatch.setattr(home_prepared, 'prepare_revisit', lambda *args: None)

    def expire():
        if reason == 'shutdown':
            stopping[0] = True
        clock[0] += home_prepared.PREPARE_BUDGET_S + 1

    if boundary == 'reader_cleanup':
        snapshot = store.read_snapshot

        @contextmanager
        def late_cleanup(**kwargs):
            with snapshot(**kwargs) as view:
                yield view
            # The real publication and reader cleanup have completed. Its
            # payload must survive the calculation scope's final deadline check.
            expire()

        monkeypatch.setattr(store, 'read_snapshot', late_cleanup)
    else:
        publish = store.rec_proposals.publish_prepared_home

        def expire_before_commit(*args, **kwargs):
            publish(*args, **kwargs)
            # SQL has run inside the real publication transaction; the final
            # cancellation check must still roll it back before commit.
            expire()

        monkeypatch.setattr(store.rec_proposals, 'publish_prepared_home', expire_before_commit)

    result = None
    if reason == 'shutdown':
        with pytest.raises(activity.WorkClosing), activity.cancellable_work(lambda: stopping[0]):
            home_prepared.prepare(store, 1002)
    else:
        result = home_prepared.prepare(store, 1002)
    stored = store.rec_proposals.get_proposals(home_prepared.SURFACE)
    if boundary == 'before_commit':
        assert stored == prepared
        assert result is None
    else:
        assert stored['built_at'] == 1002
        assert stored != prepared
        if reason == 'deadline':
            assert result == stored


def test_small_preparation_batches_accumulate_alternatives_across_attempts(store, prepared, monkeypatch):
    clock = [100]
    original = home_cards.HomeCards.cards

    def one_menu(self, request):
        result = original(self, request)
        clock[0] += 6
        return result

    store.rec_proposals.put_proposals(home_prepared.SURFACE, None, 1000)
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
    tid = store.tracks.track_ids_for_videos(['v-wheelhouse-0-0'])['v-wheelhouse-0-0']
    store.tracks.set_track_genre(tid, 'House')
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


def test_explicit_result_replaces_held_menu_without_reusing_its_random_assignment(store, prepared):
    first = home_prepared.cards(store, prepared, 1000, service(store))
    assert first.protos[0]['assignment']['probability'] > 0
    explicit = deepcopy(first)
    explicit.protos[0]['tracks'].reverse()
    explicit.protos[0]['name'] = 'Explicitly rebuilt mix'
    home_prepared.remember(store, prepared, explicit, 1001)
    # A later refill of the disposable pool must not resurrect the previous offer.
    store.rec_proposals.publish_prepared_home(
        json.dumps(prepared), 1002, surface='home_tuned')
    store.conn.commit()
    again = home_prepared.cards(store, home_prepared.load(store, 1002), 1002, service(store))
    assert again.protos[0]['name'] == 'Explicitly rebuilt mix'
    assert again.protos[0]['tracks'] == explicit.protos[0]['tracks']
    assert [p['tracks'] for p in again.protos[1:]] == [p['tracks'] for p in first.protos[1:]]
    assert all(p['assignment'] is None for p in again.protos)
    assert again.choice_context == first.choice_context


def test_tuned_result_is_reused_and_expiry_restores_baseline(store, prepared):
    with mix_tuning.adjusting(store, 1000):
        store.rec_model.set_lean("genre:jazz", 2, 1000)
    result = service(store).cards(home_cards.CardsRequest(1000, record=False))
    home_prepared.remember(store, prepared, result, 1000)
    tuned = home_prepared.load(store, 1001)
    assert tuned["tuning"] == home_prepared.tuning_key(store, 1000)
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
