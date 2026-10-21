"""Saved discovery metadata changes the songs selected for a generated Fresh playlist."""
import logging
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import FakeClient
from yt_playlist.enrich.enrich_worker import EnrichWorker
from yt_playlist.library import executor
from yt_playlist.rec import embed, fresh_pool, mode_surfaces, recipes, surfaces
from yt_playlist.rec.scoring import PlaylistTaste
from yt_playlist.util import genre_map
from yt_playlist.util.matching import identity_key


@pytest.mark.parametrize("with_modes", [False, True])
def test_fresh_without_any_enrichment_or_artist_model_can_generate_a_playlist(store, with_modes):
    iid = store.upsert_identity("main", "cred", None, True)
    if with_modes:
        store.modes.replace_modes([
            {"mode_id": 1, "label": "House", "families": [["house", 1]],
             "centroid": np.array([1., 0.], dtype=np.float32), "size": 50, "rep_keys": []},
        ], retired_ids=[], now=1)
    keys = []
    for i in range(12):
        title, artist = f"Unenriched {i}", f"Unknown artist {i}"
        key = identity_key(title, artist)
        store.upsert_discovered_track(key, f"new{i}", title, artist, "", None,
                                      None, None, None, 100)
        keys.append(key)

    # The normal background rebuild must prepare Fresh even before modes/model coverage exist.
    mode_surfaces.prepare_bundles(store, 200)
    picked = fresh_pool.pick(store, 200)
    assert {t["key"] for t in picked} == set(keys)
    assert all(not t["genre"] and t["year"] is None for t in picked)
    assert all(r["last_enriched"] is None for r in store.get_discovered_tracks())
    # Unknown genres remain unknown; they must not pretend to satisfy an explicit genre request.
    assert fresh_pool.pick(store, 200, include_genres=["house"]) == []

    client = FakeClient()
    result = executor.create_generated_playlist(store, "Fresh songs", picked, client, 200, iid,
                                                recipe={"model": "fresh"})
    assert result["added"] == 12
    assert set(client.added[0][1]) == {f"new{i}" for i in range(12)}


@pytest.mark.parametrize("signal", ["genre", "year", "audio"])
def test_enrichment_updates_fresh_ranking_modes_and_generated_playlist(store, monkeypatch, signal):
    # Fit the real projection on two playlist clusters. Only the named metadata field
    # separates them, so a ranking/mode change cannot come from another signal.
    a = {"genre": "Techno", "year": "2024", "audio": {}}
    b = dict(a)
    if signal == "genre":
        b["genre"] = "Folk"
    elif signal == "year":
        b["year"] = "1994"
    else:
        a["audio"] = {"bpm": 172.0, "energy": .92, "danceability": .88}
        b["audio"] = {"bpm": 88.0, "energy": .18, "danceability": .20}

    iid = store.upsert_identity("main", "cred", None, True)
    groups = []
    for group, fields in (("A", a), ("B", b)):
        tids = [store.upsert_track(f"{group}{i}", f"{group}{i}", group, None, 200) for i in range(10)]
        for tid in tids:
            store.set_track_genre(tid, fields["genre"])
            store.set_track_year(tid, fields["year"])
            if fields["audio"]:
                store.set_track_audio(tid, **fields["audio"])
        pid = store.upsert_playlist(iid, group, group, 10, group, 1)
        store.set_playlist_tracks(pid, tids)
        store.mark_enriched(tids, 200)
        groups.append(sorted(store.get_playlist_track_keys(pid)))
    embed.build_and_store(store, dim=8)
    embed.build_content_and_store(store)
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
    store.put_proposals("mode_bundles", {
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
            store.upsert_discovered_track(key, f"{prefix}{i}", title, artist, "", None,
                                          b["genre"], b["year"], "genre:techno", 100,
                                          audio=b["audio"])
            target.append(key)
    store.mark_discovered_enriched(reference, 200)
    embed.build_discovered_content_vectors(store)
    fresh_pool.prepare(store, 200)
    assert [t["key"] for t in fresh_pool.pick(store, 200, size=4)] == reference

    def waterfall(s, batch, cfg, on_progress, should_stop=None, sink_for=None):
        for track in batch:
            sink = sink_for(track)
            sink.set_enrichment(a["genre"], a["year"])
            if a["audio"]:
                sink.set_audio(**a["audio"])
        return changed

    worker = EnrichWorker(SimpleNamespace(store=store, now_fn=lambda: 500,
                                         logger=logging.getLogger(__name__)), waterfall_fn=waterfall)
    assert worker.drain_once(limit=4) == 4
    picked = fresh_pool.pick(store, 500, size=4)
    assert {t["key"] for t in picked} == set(changed)
    bundles = store.get_proposals("mode_bundles")
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
    result = executor.create_generated_playlist(store, "Fresh songs", picked, client, 500, iid,
                                                recipe={"model": "fresh"})
    assert result["added"] == 4
    assert set(client.added[0][1]) == {f"Changed{i}" for i in range(4)}
