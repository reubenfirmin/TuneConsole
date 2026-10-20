"""Tools › Enrichment page: render, live stats fragment, pause toggle."""
from fastapi.testclient import TestClient
import pytest

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    return TestClient(app, base_url="http://127.0.0.1")


def _seed(store):
    t1 = store.upsert_track("v1", "A", "X", None, 200)
    t2 = store.upsert_track("v2", "B", "Y", None, 200)
    store.set_track_enrichment(t1, "Rock", "1999")
    store.mark_enriched([t1], now=5.0)                 # 1 of 2 processed, has genre+year
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
    assert "Conflicts do not block the queue" in html


def test_one_queue_counts_and_pages_library_and_discovery_in_shared_order(store):
    store.upsert_track("lib", "Library song", "Artist", None, 200, created_at=39.5)
    for i in range(41):
        store.upsert_discovered_track(f"new{i}|artist", f"new{i}", f"Discovery song {i}", "Artist",
                                      None, None, None, None, "radio:seed", float(i))
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
    store.upsert_discovered_track("new|artist", "new", "New", "Artist", None, None,
                                  None, None, "radio:seed", 100.0)
    store.mark_discovered_enriched(["new|artist"], 998.0)
    html = _client(store).get("/enrich/stats").text
    assert "Last track completed 2s ago" in html
    assert "1 tracks checked in the past 15 minutes" in html
    assert "1 of 1 tracks checked" in html and '>100%</span>' in html
    processed = _client(store).get("/enrich/processed").text
    assert "New" in processed and "2s ago" in processed


def test_toggle_pauses_and_resumes(store):
    _seed(store)
    c = _client(store)
    assert store.get_setting("enrich_worker_enabled", "1") == "1"
    frag = c.post("/enrich/toggle").text
    assert store.get_setting("enrich_worker_enabled") == "0"   # paused
    assert "Resume worker" in frag and "Paused" in frag
    c.post("/enrich/toggle")
    assert store.get_setting("enrich_worker_enabled") == "1"   # resumed


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


def test_no_providers_explains_why_queue_cannot_run(store):
    from yt_playlist.providers import enrichment
    _seed(store)
    enrichment.save_config(store, [{"name": p["name"], "enabled": False}
                                   for p in enrichment.PROVIDERS])
    c = _client(store)
    assert c.app.state.ctx.enrich_worker.drain_once() == 0
    html = c.get("/enrich/stats").text
    assert "Needs setup" in html and "No enrichment providers are available" in html
    assert 'href="/setup?tab=enrichment"' in html
    assert store.queue_remaining() == 1


@pytest.mark.parametrize("view", ["queue", "processed", "conflicts"])
def test_detail_empty_states_and_page_validation(store, view):
    c = _client(store)
    assert c.get(f"/enrich/{view}").status_code == 200
    assert c.get(f"/enrich/{view}?page=0").status_code == 422
    assert c.get(f"/enrich/{view}?page=999").status_code == 200


def test_queue_and_processed_details_are_separate_and_paginated(store):
    ids = [store.upsert_track(f"vid{i}", f"Queue song {i:02}", "Artist", None, 200,
                              created_at=1.0) for i in range(41)]
    done = store.upsert_track("done", "Completed song", "Artist", None, 200)
    store.mark_enriched([done], 995.0)
    c = _client(store)
    first = c.get("/enrich/queue").text
    second = c.get("/enrich/queue?page=2").text
    assert "Queue song 00" in first and "Queue song 40" not in first
    assert "Queue song 40" in second and "Queue song 00" not in second
    assert "Completed song" not in first + second
    processed = c.get("/enrich/processed").text
    assert "Completed song" in processed and "Queue song" not in processed
    assert "No core fields found" in processed and "5s ago" in processed
    assert [t["id"] for t in store.next_enrich_batch(1, 40)] == [ids[-1]]


def test_conflicts_show_current_and_provider_values_and_resolve_explicit_choice(store):
    tid, other = _seed(store)
    store.upsert_conflict(tid, "genre", [{"provider": "musicbrainz", "value": "Rock"},
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
    assert store.get_track_enrichment(tid)[0] == "Rock"
    result = c.post(f"/enrich/conflicts/{tid}/genre", data={"value": "Pop"})
    assert result.status_code == 200 and "No open conflicts" in result.text
    assert store.get_track_enrichment(tid)[0] == "Pop"
    assert store.queue_remaining() == 1  # reviewing conflicts never stalls or requeues work


def test_click_save_returns_confirmation_and_allows_another_choice(store):
    tid, _ = _seed(store)
    store.upsert_conflict(tid, "genre", [{"provider": "musicbrainz", "value": "Rock"},
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
        assert store.get_track_enrichment(tid)[0] == choice
        assert store.outstanding_conflicts() == 0


def test_current_value_outside_provider_candidates_can_be_kept(store):
    tid, _ = _seed(store)
    store.set_track_genre(tid, "My own genre")
    store.upsert_conflict(tid, "genre", [{"provider": "musicbrainz", "value": "Rock"},
                                         {"provider": "discogs", "value": "Pop"}])
    c = _client(store)
    body = c.get("/enrich/conflicts").text
    assert 'value="My own genre"' in body
    assert "Saved locally; original source not recorded" in body
    response = c.post(f"/enrich/conflicts/{tid}/genre", data={"value": "My own genre"},
                      headers={"HX-Request": "true"})
    assert response.status_code == 200 and "Saved" in response.text
    assert store.get_track_enrichment(tid)[0] == "My own genre"


def test_conflict_review_is_paginated(store):
    for i in range(21):
        tid = store.upsert_track(f"vid{i}", f"Conflict song {i:02}", "Artist", None, 200)
        store.upsert_conflict(tid, "genre", [{"provider": "a", "value": "Rock"},
                                             {"provider": "b", "value": "Pop"}])
    c = _client(store)
    assert "Conflict song 20" not in c.get("/enrich/conflicts").text
    second = c.get("/enrich/conflicts?page=2").text
    assert "Conflict song 20" in second and "Conflict song 00" not in second
