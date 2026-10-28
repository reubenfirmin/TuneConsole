"""Exercise the serving/outcome ledger through real HTTP actions, including failed writes."""
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.rec import measurement, scorecard, surfaces
from yt_playlist.web.app import create_app


@pytest.fixture
def measured(store):
    iid = store.identities.upsert_identity("Main", "cred", None, True)
    ids, vectors = [], []
    for vid, title, xy in [("seed", "Seed", [1., 0.]), ("near", "Near", [.9, .1]),
                           ("far", "Far", [0., 1.])]:
        tid = store.tracks.upsert_track(vid, title, "Artist", None, 200)
        store.tracks.set_track_genre(tid, "Techno")
        ids.append(tid)
        vectors.append((f"{title.lower()}|artist", np.asarray(xy, dtype=np.float32).tobytes()))
    pid = store.playlists.upsert_playlist(iid, "PL", "Library", 1, "h", 0)
    store.playlists.set_playlist_tracks(pid, ids[:1])
    other = store.playlists.upsert_playlist(iid, "OTHER", "Other", 2, "h", 0)
    store.playlists.set_playlist_tracks(other, ids)
    store.rec_model.replace_rec_vectors(vectors)
    store.discovery.upsert_discovered_track("cold|new artist", "cold", "Cold", "New artist", "", None,
                                  "Techno", "2000", "source", 0)
    remote, clock = FakeClient(), [100.]
    app = create_app(store, lambda: {iid: remote}, now_fn=lambda: clock[0])
    with TestClient(app, base_url="http://127.0.0.1") as http:
        yield store, http, app.state.ctx, remote, clock, iid, pid


def test_final_recommendations_preserve_scores_order_context_and_policy(measured):
    store, http, _, _, _, _, pid = measured
    # Search/poll endpoints and an ordinary table never create recommendation impressions.
    http.get("/clusters/search", params={"q": "Artist"})
    http.get(f"/playlist/{pid}/cells")
    assert store.conn.execute("SELECT COUNT(*) FROM rec_served_impressions").fetchone()[0] == 0

    fragment = http.get(f"/playlist/{pid}/suggestions")
    assert fragment.status_code == 200 and "Near" in fragment.text
    expected = surfaces.complete_playlist(store, pid, now=100)
    rows = store.conn.execute("SELECT * FROM rec_served_impressions ORDER BY id").fetchall()
    assert [r["identity_key"] for r in rows] == [i.key for i in expected]
    assert [r["score"] for r in rows] == [i.score for i in expected]
    assert all(r["score"] is not None for r in rows)
    assert json.loads(rows[0]["provenance"])["playlist_id"] == pid

    response = http.get("/track/seed/similar")
    assert response.status_code == 200 and "Near" in response.text
    ring = http.post("/clusters/expand", json={"pos_keys": ["seed|artist"], "k": 2}).json()["ring"]
    cluster_rows = store.conn.execute("SELECT * FROM rec_served_impressions WHERE surface='clusters' ORDER BY rank").fetchall()
    assert [(r["identity_key"], r["rank"], r["score"]) for r in cluster_rows] == [
        (item["key"], rank, item["score"]) for rank, item in enumerate(ring, 1)]
    trace = http.get(f"/taste/served/{cluster_rows[0]['id']}").json()
    assert trace["configuration"]["ranker"] == "cluster-expand"
    assert trace["provenance"]["positive_seeds"] == ["seed|artist"]
    similar = store.conn.execute("SELECT * FROM rec_served_impressions WHERE surface='similar' ORDER BY rank").fetchall()
    assert similar[0]["identity_key"] == "near|artist" and similar[0]["score"] > 0


@pytest.mark.parametrize("generated", [False, True])
def test_save_counts_only_successful_adoption_and_promotion_preserves_play_provenance(measured, monkeypatch, generated):
    store, http, _, remote, clock, iid, pid = measured
    if generated:
        store.playlists.set_playlist_group("PL", "Generated")
        store.playlists.mark_generated_playlist('PL')
    measurement.record_recommendations(store, "home", [{"key": "cold|new artist", "score": .8}], 100)
    clock[0] = 110
    body = {"playlist_id": pid, "song": {"video_id": "cold", "title": "Cold", "artist": "New artist"}}
    original_add = remote.add_playlist_items

    def fail(*args):
        raise RuntimeError("remote failed")

    monkeypatch.setattr(remote, "add_playlist_items", fail)
    response = http.post("/songs/add-to-playlist", json=body)
    assert response.status_code == 200
    assert response.json()["status"] == "partial"
    assert response.json()["added"] is False
    assert response.json()["uncertain"] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM rec_response_events").fetchone()[0] == 0
    monkeypatch.setattr(remote, "add_playlist_items", original_add)
    assert http.post("/songs/add-to-playlist", json=body).status_code == 200
    assert http.post("/songs/add-to-playlist", json=body).status_code == 200  # already present
    assert store.conn.execute("SELECT COUNT(*) FROM rec_response_events").fetchone()[0] == 1
    score = scorecard.recommendation_scorecard(store, 90, 105)
    assert score["save"] == 1 and score["conversion"] == int(not generated)
    if generated:
        store.history.record_play_event(iid, "cold|new artist", "cold", 115, playlist_ytm_id="RDAMPLPL")
        clock[0] = 120
        assert http.post(f"/playlist/{pid}/promote").status_code == 200
        http.post(f"/playlist/{pid}/promote")
        score = scorecard.recommendation_scorecard(store, 90, 105)
        assert score["promotion"] == score["conversion"] == score["generated"] == 1
        assert score["organic"] == 0


def test_open_start_and_scorecard_range_are_observed_without_inventing_a_play(measured, monkeypatch):
    store, http, ctx, _, clock, _, pid = measured
    measurement.record_recommendations(store, "similar", [{"key": "seed|artist", "score": .5}], 100)
    clock[0] = 110
    assert http.get(f"/playlist/{pid}").status_code == 200
    # A disconnected extension must not count as a successful request.
    assert http.post("/play", json={"url": "https://music.youtube.com/watch?v=seed"}).json()["ok"] is False
    assert scorecard.recommendation_scorecard(store, 90, 105)["start"] == 0
    monkeypatch.setattr(ctx.bridge, "send_control", lambda frame, *, intent: True)
    response = http.post("/play", json={"url": "https://music.youtube.com/watch?list=RDAMPLPL"}).json()
    assert response["ok"] is True, response
    score = http.get("/taste/scorecard", params={"start": 90, "end": 105}).json()
    assert score["open"] == score["start"] == 1
    assert score["played"] == 0 and score["verdict"] is None
    trace_id = store.conn.execute("SELECT id FROM rec_served_impressions").fetchone()[0]
    trace = http.get(f"/taste/served/{trace_id}").json()
    assert trace["outcomes"]["start"] == 1 and trace["outcomes"]["played"] == 0
    assert http.get("/taste/served/999999").status_code == 404
    for start, end in [(105, 90), (-1, 100), (0, 91 * 86400), (100, 100)]:
        assert http.get("/taste/scorecard", params={"start": start, "end": end}).status_code == 422


def test_cluster_save_is_a_generated_response_and_not_discovery_conversion(measured):
    store, http, _, _, clock, _, _ = measured
    measurement.record_recommendations(store, "clusters", [{"key": "cold|new artist"}], 100)
    clock[0] = 110
    result = http.post("/clusters/save", data={"name": "Cluster", "keep_keys": '["cold|new artist"]',
                                               "central_keys": "[]"})
    assert result.status_code == 200 and "Saved" in result.text
    score = scorecard.recommendation_scorecard(store, 90, 105)
    assert score["save"] == 1 and score["conversion"] == 0
    html = http.get("/taste/recall").text
    assert "1 saved" in html and "Coverage and response samples" in html
    assert "Cold tracks" in html and "collecting evidence" in html


def test_migration_from_previous_schema_preserves_old_impressions_and_new_ledger_on_reopen(tmp_path):
    from yt_playlist.core.migrations import migrate
    from yt_playlist.core.migrations.registry import MIGRATIONS
    from yt_playlist.core.store import Store

    path = tmp_path / "measurement.db"
    store = Store(path)
    migrate(store.conn, registry=MIGRATIONS[:8])
    # Seed the previous schema using its own columns, before policy bases exist.
    with store.conn:
        store.conn.execute(
            'INSERT INTO rec_served_impressions '
            '(request_id,surface,lane,identity_key,rank,model_version,provenance,served_at) '
            'VALUES (?,?,?,?,?,?,?,?)',
            ('legacy', 'home', '', 'old|artist', 1, 'legacy', '{}', 50))
    store.init_schema()
    measurement.record_recommendations(store, "home", [{"key": "new|artist", "score": .4}], 100)
    store.rec_ledger.record_recommendation_outcomes("save", "save", ["new|artist"], 110)
    store.conn.close()
    reopened = Store(path)
    reopened.init_schema()
    score = scorecard.recommendation_scorecard(reopened, 40, 105)
    assert score["impressions"] == 2 and score["save"] == 1
    trace = scorecard.served_recommendation(reopened, 2)
    assert trace["configuration"]["algorithm"] == "served-ranking-v2"
    assert score["by_ownership"]["unknown"]["impressions"] == 2
    reopened.conn.close()
