"""Saved discovery metadata queues background preparation with the established models."""
import logging
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import FakeClient
from yt_playlist.enrich.enrich_worker import EnrichWorker
from yt_playlist.library import playlist_generated
from yt_playlist.rec import content_model, embedding_training as training, embedding_vectors as embed, fresh_pool, mode_surfaces, model_refresh, recipes, surfaces
from yt_playlist.rec.scoring import PlaylistTaste
from yt_playlist.util import genre_map
from yt_playlist.util.matching import identity_key


@pytest.mark.parametrize("with_modes", [False, True])
def test_fresh_waits_for_metadata_then_generates_a_playlist_without_a_model(store, with_modes):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    if with_modes:
        store.modes.replace_modes([
            {"mode_id": 1, "label": "House", "families": [["house", 1]],
             "centroid": np.array([1., 0.], dtype=np.float32), "size": 50, "rep_keys": []},
        ], retired_ids=[], now=1)
    keys = []
    for i in range(12):
        title, artist = f"Unenriched {i}", f"Unknown artist {i}"
        key = identity_key(title, artist)
        store.discovery.upsert_discovered_track(key, f"new{i}", title, artist, "", None,
                                      None, None, None, 100)
        keys.append(key)

    # Discovery alone supplies no ranking evidence. Keep the tracks queued for enrichment.
    mode_surfaces.prepare_bundles(store, 200)
    assert fresh_pool.pick(store, 200) == []
    pending = store.discovery.get_discovered_tracks()
    assert {r["identity_key"] for r in pending} == set(keys)
    assert all(r["last_enriched"] is None for r in pending)

    # Metadata makes an unscored fallback eligible even before a taste/artist model exists.
    for key in keys:
        store.discovery.set_discovered_enrichment(key, None, "2000")
    fresh_pool.prepare(store, 250)
    picked = fresh_pool.pick(store, 250)
    assert {t["key"] for t in picked} == set(keys)
    assert all(not t["genre"] and t["year"] == 2000 and t["score"] is None for t in picked)
    # Unknown genres remain unknown; they must not pretend to satisfy an explicit genre request.
    assert fresh_pool.pick(store, 250, include_genres=["house"]) == []

    client = FakeClient()
    result = playlist_generated.create_generated_playlist(store, "Fresh songs", picked, client, 250, iid,
                                                recipe={"model": "fresh"})
    assert result["added"] == 12
    assert set(client.added[0][1]) == {f"new{i}" for i in range(12)}


@pytest.mark.parametrize("signal", ["genre", "year", "audio"])
def test_enrichment_updates_fresh_modes_and_generated_playlist(store, monkeypatch, signal):
    # Fit the real projection on two playlist clusters. Only the named metadata field
    # separates them, so a placement change cannot come from another signal.
    a = {"genre": "Techno", "year": "2024", "audio": {}}
    b = dict(a)
    if signal == "genre":
        b["genre"] = "Folk"
    elif signal == "year":
        b["year"] = "1994"
    else:
        a["audio"] = {"bpm": 172.0, "energy": .92, "danceability": .88}
        b["audio"] = {"bpm": 88.0, "energy": .18, "danceability": .20}

    iid = store.identities.upsert_identity("main", "cred", None, True)
    groups = []
    for group, fields in (("A", a), ("B", b)):
        tids = [store.tracks.upsert_track(f"{group}{i}", f"{group}{i}", group, None, 200) for i in range(10)]
        for tid in tids:
            store.tracks.set_track_genre(tid, fields["genre"])
            store.tracks.set_track_year(tid, fields["year"])
            if fields["audio"]:
                store.tracks.set_track_audio(tid, **fields["audio"])
        pid = store.playlists.upsert_playlist(iid, group, group, 10, group, 1)
        store.playlists.set_playlist_tracks(pid, tids)
        store.enrichment_queue.mark_enriched(tids, 200)
        groups.append(sorted(store.playlists.get_playlist_track_keys(pid)))
    store.settings.set_setting("rec_dim", "8")
    assert model_refresh.rebuild(store, 200) is not None
    _, vectors, idx = embed.load_vectors(store)
    preferred = vectors[[idx[k] for k in groups[0]]].mean(0)
    preferred /= np.linalg.norm(preferred)
    monkeypatch.setattr(surfaces, "playlist_taste", lambda s: PlaylistTaste(
        ["Preferred playlist"], preferred[None, :], np.array([1.0])))

    _, content, cidx = embed.load_content_vectors(store)
    modes = []
    for mid, keys, fields in zip((1, 2), groups, (a, b)):
        centroid = content[[cidx[k] for k in keys]].mean(0)
        centroid /= np.linalg.norm(centroid)
        modes.append({"mode_id": mid, "label": f"Mode {mid}", "centroid": centroid,
                      "space": embed.content_space_id(store), "size": 10000 if mid == 1 else 10,
                      "families": [[genre_map.family(fields["genre"]), 10]], "rep_keys": keys[:4]})
    store.modes.replace_modes(modes, retired_ids=[], now=200)
    store.rec_proposals.put_proposals("mode_bundles", {
        "1": {"wheelhouse": [], "explore": [], "comfort": [], "fresh": []},
        "2": {"wheelhouse": [], "explore": [], "comfort": [], "fresh": []},
        "all": {"wheelhouse": [], "explore": [], "comfort": [], "fresh": []},
        "_meta": {"full_pools": True, "comfort_pool": 0, "year_cuts": None},
    }, 200)
    reference, changed = [], []
    for prefix, target in (("Reference", reference), ("Changed", changed)):
        for i in range(4):
            title, artist = f"{prefix} {i}", f"New {prefix} artist {i}"
            key = identity_key(title, artist)
            store.discovery.upsert_discovered_track(key, f"{prefix}{i}", title, artist, "", None,
                                          b["genre"], b["year"], "genre:techno", 100,
                                          audio=b["audio"])
            target.append(key)
    store.discovery.mark_discovered_enriched(reference, 200)
    embed.build_discovered_content_vectors(store)
    fresh_pool.prepare(store, 200)
    assert [t["key"] for t in fresh_pool.pick(store, 200, size=4)] == reference
    old_order = [t["key"] for t in store.rec_proposals.get_proposals("fresh_pool")]
    model = store.settings.get_setting("rec_content_model")

    def forbidden(*args, **kwargs):
        raise AssertionError("An enrichment batch must reuse saved models and rankings")

    monkeypatch.setattr(training, "build_vectors_from_baskets", forbidden)
    monkeypatch.setattr(content_model, "build_library_content", forbidden)
    monkeypatch.setattr(fresh_pool, "prepare", forbidden)

    def waterfall(s, batch, cfg, on_progress, should_stop=None, sink_for=None, **kwargs):
        for track in batch:
            sink = sink_for(track)
            sink.apply_fields("musicbrainz", {"genre": a["genre"], "year": a["year"]})
            if a["audio"]:
                sink.apply_fields("acousticbrainz", a["audio"])
        return [track['job_id'] for track in batch]

    worker = EnrichWorker(SimpleNamespace(store=store, now_fn=lambda: 500,
                                         logger=logging.getLogger(__name__)), waterfall_fn=waterfall)
    assert worker.drain_once(limit=4) == 4
    assert [t["key"] for t in store.rec_proposals.get_proposals("fresh_pool")] == old_order
    assert store.settings.get_setting("rec_content_model") == model
    assert [t["key"] for t in fresh_pool.pick(store, 500, size=4)] == reference
    assert store.rec_refresh.has_pending_changes()
    # Enrichment only notifies the worker. The background pass reuses the library
    # encoder and original ranker before publishing the updated discovery pools.
    assert model_refresh.rebuild(store, 500) is not None
    assert mode_surfaces.prepare_bundles(store, 500) is not None
    assert store.settings.get_setting("rec_content_model") == model
    bundles = store.rec_proposals.get_proposals("mode_bundles")
    assert {t["key"] for t in bundles["1"]["fresh"]} == set(changed)
    assert {t["key"] for t in bundles["2"]["fresh"]} == set(reference)
    assert all(t["year"] == 2024 for t in bundles["1"]["fresh"])
    # Focus this preview on the preferred mode; mode rotation is tested separately.
    monkeypatch.setattr(mode_surfaces, "select_modes", lambda *args, **kwargs: [1])
    cards = mode_surfaces.assemble_cards(store, 500, epoch=0)
    fresh = next(c for c in cards if c["lane"] == "fresh")
    assert {t["key"] for t in fresh["tracks"][:4]} == set(changed)
    if signal == "year":
        ordered = recipes.theme_filter(store, list(reversed(bundles["all"]["fresh"])), {"eras": ["2020"]})
        assert {t["key"] for t in ordered[:4]} == set(changed)

    client = FakeClient()
    picked = fresh["tracks"][:4]
    result = playlist_generated.create_generated_playlist(store, "Fresh songs", picked, client, 500, iid,
                                                recipe={"model": "fresh"})
    assert result["added"] == 4
    assert set(client.added[0][1]) == {f"Changed{i}" for i in range(4)}
