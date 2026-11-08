"""Tools › Enrichment page: render, live stats fragment, pause toggle."""
from fastapi.testclient import TestClient
from tests.enrichment_support import complete_entries, complete_discovered, seed_enrichment
from tests.metadata_provenance_support import metadata_provenance
import pytest

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient
from tests.store_fixture_support import bounded_consumer_schema, file_consumer_store as store


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('rock', 'Rock'), ('pop', 'Pop'))) as schema:
        yield schema


@pytest.fixture(autouse=True)
def enrichment_routes_only(monkeypatch, enrichment_template_bytecode):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview, enrich

    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [
        app_update.build(ctx), audio_preview.build(ctx), enrich.build(ctx)])
    templates = webapp.Jinja2Templates

    def compiled_templates(*args, **kwargs):
        result = templates(*args, **kwargs)
        result.env.bytecode_cache = enrichment_template_bytecode
        return result

    monkeypatch.setattr(webapp, 'Jinja2Templates', compiled_templates)


@pytest.fixture(scope='module')
def enrichment_template_bytecode(tmp_path_factory):
    from jinja2 import FileSystemBytecodeCache

    return FileSystemBytecodeCache(str(tmp_path_factory.mktemp('enrichment-bytecode')))


def _client(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0,
                     background_work=False)
    return TestClient(app, base_url="http://127.0.0.1", backend_options={'use_uvloop': True})


def _seed(store):
    t1 = store.tracks.upsert_track("v1", "A", "X", None, 200)
    t2 = store.tracks.upsert_track("v2", "B", "Y", None, 200)
    seed_enrichment(store, t1, "Rock", "1999")
    complete_entries(store, [t1], now=5.0)                 # 1 of 2 processed, has genre+year
    return t1, t2


def test_enrich_page_renders_coverage(store):
    _seed(store)
    html = _client(store).get("/enrich").text
    assert "Enrichment" in html
    for label in ("Processed", "Genre", "Year", "BPM", "Energy", "Danceability"):
        assert label in html
    assert "In queue" in html and "Open conflicts" in html


def test_stats_fragment_reflects_state(store):
    _seed(store)
    html = _client(store).get("/enrich/stats").text
    assert 'state-queued' in html
    assert 'state-running' not in html
    assert '>50%</span>' in html
    assert 'href="/enrich/queue"' in html
    assert 'href="/enrich/processed"' in html
    assert 'href="/enrich/conflicts"' in html
    assert "1 awaiting first enrichment" in html


def test_queue_distinguishes_first_passes_from_refreshes_without_double_counting(store):
    _seed(store)
    store.discovery.upsert_discovered_track("a|x", "v1", "A", "X", None, None,
                                           None, None, "radio:seed", 10.0)
    configuration = {name: {"fingerprint": "old", "status": "available"}
                     for name in ("musicbrainz", "lastfm")}
    store.enrichment_state.reconcile_provider_configuration(configuration)
    store.enrichment_state.reconcile_provider_configuration({
        name: {**value, "fingerprint": "new"} for name, value in configuration.items()})
    assert store.enrichment_reports.queue_stats() == {"total": 2, "first_pass": 1, "refresh": 1}
    c = _client(store)
    for path in ("/enrich", "/enrich/stats"):
        html = c.get(path).text
        assert "1 awaiting first enrichment" in html
        assert "1 queued for re-enrichment" in html
    detail = c.get("/enrich/queue").text
    assert ">First enrichment</td>" in detail and ">Re-enrichment</td>" in detail
    store.enrichment_state.reconcile_provider_configuration({
        name: {**value, "status": "disabled"} for name, value in configuration.items()})
    assert store.enrichment_reports.queue_stats() == {"total": 1, "first_pass": 1, "refresh": 0}


def test_saved_retry_status_is_visible_without_a_running_worker(store):
    tid = store.tracks.upsert_track("retry", "Retry", "Artist", None, 200)
    track = store.tracks.catalog_subject(tid)
    store.enrichment_state.record_enrichment_attempt(track["identity_key"], "musicbrainz", {}, retry=True,
        now=990, error="bad <response>", retry_after=1090)
    html = _client(store).get("/enrich/stats").text
    assert 'state-retry' in html
    assert "Next attempt in 90 seconds" in html
    assert "bad &lt;response&gt;" in html
    assert "0 of 1 tracks checked" in html


def test_optional_artwork_retry_is_visible_without_reopening_metadata_queue(store):
    from yt_playlist.providers.coverart import CoverResult
    tid = store.tracks.upsert_track("art", "Art", "Artist", None, 200)
    complete_entries(store, [tid], now=900)
    store.enrichment_state.record_artwork_result(tid, ["release"], CoverResult("retry", retry_in=90), 990)
    html = _client(store).get("/enrich/stats").text
    assert "Artwork retries: 1" in html
    assert "Next attempt in 80 seconds" in html
    assert 'state-idle' in html
    assert "1 of 1 tracks checked" in html


def test_one_queue_counts_and_pages_library_and_discovery_in_shared_order(store):
    from tests.recommendation_support import seed_publication_subjects

    discoveries = [{'key': f'new{i}|artist', 'video_id': f'new{i}',
                    'title': f'Discovery song {i}', 'artist': 'Artist', 'created_at': float(i)}
                   for i in range(41)]
    # Queue ordering consumes completed ingestion, with genuine library and
    # discovery memberships and interleaved recording creation times.
    seed_publication_subjects(store, [
        {'key': 'library song|artist', 'video_id': 'lib', 'title': 'Library song',
         'artist': 'Artist', 'created_at': 39.5}, *discoveries],
        discoveries=[row['key'] for row in discoveries])
    c = _client(store)
    stats = c.get("/enrich/stats").text
    assert '<span class="v">42</span>' in stats
    assert "scope=discovery" not in stats
    first = c.get("/enrich/queue").text
    assert "Discovery song 40" in first and "Library song" in first
    assert first.index("Discovery song 40") < first.index("Library song") < first.index("Discovery song 39")
    assert 'page=2"' in first and "Enrichment queues" not in first
    second = c.get("/enrich/queue?page=2").text
    assert "Discovery song 0" in second and "Discovery song 1" in second
    assert "Library song" not in second
    # Previously shared bookmarks cannot select a separate queue anymore.
    assert "Library song" in c.get("/enrich/queue?scope=discovery").text


def test_discovery_completions_appear_in_live_progress(store):
    store.discovery.upsert_discovered_track("new|artist", "new", "New", "Artist", None, None,
                                  None, None, "radio:seed", 100.0)
    complete_discovered(store, ["new|artist"], 998.0)
    html = _client(store).get("/enrich/stats").text
    assert "Last track completed 2s ago" in html
    assert "1 tracks checked in the past 15 minutes" in html
    assert "1 of 1 tracks checked" in html and '>100%</span>' in html
    processed = _client(store).get("/enrich/processed").text
    assert "New" in processed and "2s ago" in processed


def test_toggle_pauses_and_resumes(store):
    _seed(store)
    c = _client(store)
    assert store.settings.get_setting("enrich_worker_enabled", "1") == "1"
    frag = c.post("/enrich/toggle").text
    assert store.settings.get_setting("enrich_worker_enabled") == "0"   # paused
    assert "Resume worker" in frag and "Paused" in frag
    c.post("/enrich/toggle")
    assert store.settings.get_setting("enrich_worker_enabled") == "1"   # resumed


def test_live_worker_activity_is_visible(store):
    _seed(store)
    c = _client(store)
    worker = c.app.state.ctx.enrich_worker
    worker._busy = True
    worker._record_activity(state="running", text="Looking up B with Discogs.")
    html = c.get("/enrich/stats").text
    assert 'state-running' in html
    assert "Looking up B with Discogs." in html
    assert "Updated 0s ago" in html
    worker._record_activity(state="waiting", text="Waiting for a newer lookup.")
    assert 'state-waiting' in c.get("/enrich/stats").text


@pytest.mark.parametrize("availability", ["disabled", "unavailable"])
def test_no_providers_explains_why_queue_cannot_run(store, monkeypatch, availability):
    from functools import partial
    from unittest.mock import Mock
    from yt_playlist.enrich import artwork
    from yt_playlist.providers import enrichment
    from yt_playlist.providers.base import EnrichmentResult
    from yt_playlist.providers.waterfall import run_waterfall

    checked, pending = _seed(store)
    probed = []

    class Provider:
        name = "musicbrainz"
        ready = availability == "disabled"
        def available(self, _store): return self.ready
        def tripped(self): return False
        def probe(self, track, _store):
            probed.append(track["id"])
            return EnrichmentResult(self.name, {"genre": "Rock", "year": "2004"})

    provider = Provider()
    enrichment.save_config(store, [{"name": p["name"],
                                    "enabled": availability == "unavailable" and p["name"] == provider.name}
                                   for p in enrichment.PROVIDERS])
    c = _client(store)
    worker = c.app.state.ctx.enrich_worker
    worker.waterfall_fn = partial(run_waterfall, registry={provider.name: provider})
    art_retry = Mock(return_value=0)
    monkeypatch.setattr(artwork, "retry_due", art_retry)
    def timestamps():
        return [tuple(row) for row in store.conn.execute(
            "SELECT id,first_enriched_at,last_enriched_at FROM tracks ORDER BY id")]
    before = timestamps()
    assert worker.drain_once() == 0
    html = c.get("/enrich/stats").text
    assert "Needs setup" in html and "No enrichment providers are available" in html
    assert 'href="/setup?tab=enrichment"' in html
    assert store.enrichment_reports.queue_remaining() == 1
    assert timestamps() == before and not probed and not worker.busy
    art_retry.assert_called_once()
    # Optional artwork remains independent of provider setup and metadata completion.
    art_retry.return_value = 1
    assert worker.drain_once() == 1
    assert timestamps() == before and store.enrichment_reports.queue_remaining() == 1
    assert 'state-blocked' in c.get("/enrich/stats").text
    assert 'state-paused' in c.post("/enrich/toggle").text
    assert worker.drain_once() == 0
    assert art_retry.call_count == 2
    c.post("/enrich/toggle")
    assert 'state-blocked' in c.get("/enrich/stats").text
    provider.ready = True
    enrichment.save_config(store, [{"name": p["name"], "enabled": p["name"] == provider.name}
                                   for p in enrichment.PROVIDERS])
    art_retry.return_value = 0
    assert worker.drain_once(limit=1) == 1
    assert probed == [pending]
    assert store.tracks.get_track_enrichment(pending) == ("Rock", "2004")
    assert timestamps()[1][1:] == (1000.0, 1000.0)
    assert "Needs setup" not in c.get("/enrich/stats").text
    # Re-enabling also schedules ordinary provider coverage for the previously checked track.
    assert worker.drain_once() == 1
    assert probed == [pending, checked]
    assert timestamps()[0][1:] == (5.0, 1000.0)
    assert worker.drain_once() == 0
    assert 'state-idle' in c.get("/enrich/stats").text


@pytest.mark.parametrize("view", ["queue", "processed", "conflicts"])
def test_detail_empty_states_and_page_validation(store, view):
    c = _client(store)
    assert c.get(f"/enrich/{view}").status_code == 200
    assert c.get(f"/enrich/{view}?page=0").status_code == 422
    assert c.get(f"/enrich/{view}?page=999").status_code == 200


def test_queue_and_processed_details_are_separate_and_paginated(store):
    from tests.recommendation_support import seed_publication_subjects

    seed_publication_subjects(store, [
        {'key': f'queue song {i:02}|artist', 'video_id': f'vid{i}',
         'title': f'Queue song {i:02}', 'artist': 'Artist', 'created_at': 1.0}
        for i in range(41)])
    ids = [row[0] for row in store.conn.execute('SELECT id FROM catalog_entries ORDER BY id')]
    done = store.tracks.upsert_track("done", "Completed song", "Artist", None, 200)
    complete_entries(store, [done], 995.0)
    c = _client(store)
    first = c.get("/enrich/queue").text
    second = c.get("/enrich/queue?page=2").text
    assert "Queue song 00" in first and "Queue song 40" not in first
    assert "Queue song 40" in second and "Queue song 00" not in second
    assert "Completed song" not in first + second
    processed = c.get("/enrich/processed").text
    assert "Completed song" in processed and "Queue song" not in processed
    assert "No core fields found" in processed and "5s ago" in processed
    assert [t["id"] for t in store.enrichment_queue.next_enrich_batch(1, 40)] == [ids[-1]]


def test_conflicts_show_current_and_provider_values_and_resolve_explicit_choice(store):
    tid, other = _seed(store)
    store.enrichment.upsert_conflict(tid, "genre", [{"provider": "musicbrainz", "value": "Rock"},
                                         {"provider": "discogs", "value": "Pop"}])
    c = _client(store)
    html = c.get("/enrich/conflicts").text
    assert 'class="conflict-choice is-current"' in html
    assert 'aria-label="Rock"' in html and ' checked>' in html
    assert 'value="Pop"' in html and 'value="Rock"' in html
    assert 'target="_blank"' in html
    assert "Reported by" in html and "MusicBrainz" in html
    assert "You can always fix these later from an album or playlist page" in html
    assert 'class="conflict-help-icon"' in html
    assert "Keep selected value" not in html
    # Bad requests cannot write arbitrary values or affect a different track.
    assert c.post(f"/enrich/conflicts/{tid}/genre", data={"value": "Invented"}).status_code == 400
    assert c.post(f"/enrich/conflicts/{other}/genre", data={"value": "Pop"}).status_code == 400
    assert c.post(f"/enrich/conflicts/{tid}/title", data={"value": "Pop"}).status_code == 400
    assert store.tracks.get_track_enrichment(tid)[0] == "Rock"
    result = c.post(f"/enrich/conflicts/{tid}/genre", data={"value": "Pop"})
    assert result.status_code == 200 and "No open conflicts" in result.text
    assert store.tracks.get_track_enrichment(tid)[0] == "Pop"
    assert store.enrichment_reports.queue_remaining() == 1  # reviewing conflicts never stalls or requeues work


def test_click_save_returns_confirmation_and_allows_another_choice(store):
    tid, _ = _seed(store)
    store.enrichment.upsert_conflict(tid, "genre", [{"provider": "musicbrainz", "value": "Rock"},
                                         {"provider": "discogs", "value": "Pop"}])
    c = _client(store)
    url = f"/enrich/conflicts/{tid}/genre"
    for choice in ("Rock", "Pop", "Rock"):
        response = c.post(url, data={"value": choice}, headers={"HX-Request": "true"})
        assert response.status_code == 200
        assert '<section class="conflict-track enrich-conflict"' in response.text
        assert "is-saved" in response.text and "Saved" in response.text
        assert "Your choice" in response.text
        assert "<html" not in response.text
        assert store.tracks.get_track_enrichment(tid)[0] == choice
        assert store.enrichment_reports.outstanding_conflicts() == 0


def _keep_current_value(store, tid, *, user_choice):
    assert metadata_provenance(store, tid)['fields']['genre'] == {
        'value':'My own genre','override':user_choice,
        'source':'user' if user_choice else 'legacy_unknown'}
    store.enrichment.upsert_conflict(tid, "genre", [{"provider": "musicbrainz", "value": "Rock"},
                                         {"provider": "discogs", "value": "Pop"}])
    c = _client(store)
    body = c.get("/enrich/conflicts").text
    assert 'value="My own genre"' in body
    assert ("Your choice" if user_choice else "Saved locally; original source not recorded") in body
    response = c.post(f"/enrich/conflicts/{tid}/genre", data={"value": "My own genre"},
                      headers={"HX-Request": "true"})
    assert response.status_code == 200 and "Saved" in response.text
    assert "Your choice" in response.text
    assert store.tracks.get_track_enrichment(tid)[0] == "My own genre"
    assert metadata_provenance(store, tid)['fields']['genre'] == {
        'value':'My own genre','override':True,'source':'conflict_resolution'}
    assert store.enrichment_reports.outstanding_conflicts() == 0


def test_current_value_outside_provider_candidates_can_be_kept(store):
    tid, _ = _seed(store)
    store.tracks.set_track_genre(tid, "My own genre")
    _keep_current_value(store, tid, user_choice=True)


def test_conflict_review_is_paginated(store):
    from tests.recommendation_support import seed_publication_subjects

    seed_publication_subjects(store, [
        {'key': f'conflict song {i:02}|artist', 'video_id': f'vid{i}',
         'title': f'Conflict song {i:02}', 'artist': 'Artist', 'duration_s': 200}
        for i in range(21)])
    for tid, in store.conn.execute('SELECT id FROM catalog_entries').fetchall():
        store.enrichment.upsert_conflict(tid, "genre", [{"provider": "a", "value": "Rock"},
                                             {"provider": "b", "value": "Pop"}])
    c = _client(store)
    assert "Conflict song 20" not in c.get("/enrich/conflicts").text
    second = c.get("/enrich/conflicts?page=2").text
    assert "Conflict song 20" in second and "Conflict song 00" not in second
