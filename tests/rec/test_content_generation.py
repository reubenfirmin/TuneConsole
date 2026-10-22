"""Content publication preserves complete generations under failures and competing encoders."""
import json
import sqlite3
import threading

import numpy as np
import pytest

from yt_playlist.core.store import Store
from yt_playlist.rec import embed, layers, mode_surfaces, ppr, rec_params, surfaces, taste_modes


def _seed(store):
    tid = store.tracks.upsert_track("owned", "Owned", "Artist", None, None)
    store.tracks.set_track_genre(tid, "Techno")
    store.discovery.upsert_discovered_track("cold|artist", "cold", "Cold", "Artist", "", None,
                                  "Techno", None, "source", 0)
    embed.build_content_and_store(store)
    return tid


def test_failed_publication_rolls_back_all_artifacts_even_after_later_commit(store):
    _seed(store)
    before = store.rec_model.content_generation()
    store.conn.execute("CREATE TRIGGER reject_content BEFORE INSERT ON rec_discovered_content_vectors "
                       "BEGIN SELECT RAISE(ABORT,'injected write failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected write failure"):
        store.rec_model.publish_content_generation([("new|artist", b"new")], [("bad|artist", b"bad")],
            "new model", "new revision", "new inputs", before["generation"])
    store.settings.set_setting("unrelated", "committed")
    assert store.rec_model.content_generation() == before


def test_discovery_encoder_retries_against_competing_library_generation(store, monkeypatch):
    tid = _seed(store)
    encode = embed._encode_discovered
    switched = False

    def competing_encode(rows, model):
        nonlocal switched
        if not switched:
            switched = True
            store.tracks.set_track_genre(tid, "Folk")
            embed.build_content_and_store(store)
        return encode(rows, model)

    monkeypatch.setattr(embed, "_encode_discovered", competing_encode)
    assert embed.build_discovered_content_vectors(store) == 0
    snapshot = store.rec_model.content_generation()
    assert "fam:folk-country" in json.loads(snapshot["model"])["cat"]
    assert snapshot["library"] and snapshot["discovered"] == []


def test_reader_sees_old_then_new_complete_generation_during_publication(tmp_path):
    writer = Store(tmp_path / "generation.db")
    writer.init_schema()
    _seed(writer)
    before = writer.rec_model.content_generation()
    reader = Store(tmp_path / "generation.db")
    reader._bind_repositories()
    writing, release = threading.Event(), threading.Event()
    errors = []

    def pause():
        writing.set()
        if not release.wait(5):
            raise RuntimeError("publication was not released")
        return 0

    writer.conn.create_function("pause_publication", 0, pause)
    writer.conn.execute("CREATE TRIGGER pause_content BEFORE INSERT ON rec_content_vectors "
                        "BEGIN SELECT pause_publication(); END")

    def publish():
        try:
            assert writer.rec_model.publish_content_generation([("new|artist", b"new library")],
                [("new cold|artist", b"new discovery")], "new model", "revision", "inputs",
                before["generation"])
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        assert writing.wait(5)
        assert reader.rec_model.content_generation() == before
    finally:
        release.set()
        thread.join(5)
    try:
        assert not thread.is_alive() and not errors
        after = reader.rec_model.content_generation()
        assert after["generation"] != before["generation"]
        assert (after["model"], after["revision"], after["inputs"]) == ("new model", "revision", "inputs")
        assert after["library"] == [("new|artist", b"new library")]
        assert after["discovered"] == [("new cold|artist", b"new discovery")]
    finally:
        writer.conn.close()
        reader.conn.close()


def _seed_remappable_space(store):
    """Two equal-size spaces whose categorical columns have opposite meanings."""
    entries = [store.tracks.upsert_track(key, key.upper(), "Artist", None, None)
               for key in ("a", "b", "c")]
    keys = ["a|artist", "b|artist", "c|artist"]
    vectors = np.array([[1., 0.], [0., 1.], [1., 0.]], dtype=np.float32)
    model = {"cat": {"genre:a": 0, "genre:b": 1}, "ncat": 2, "cont": []}
    assert store.rec_model.publish_content_generation(
        list(zip(keys, (v.tobytes() for v in vectors))), [], json.dumps(model), "rev", "old", "")
    store.modes.replace_modes([
        {"mode_id": i + 1, "label": key, "families": [], "centroid": vectors[i],
         "size": 1, "rep_keys": [key], "space": embed.content_model_fingerprint(model)}
        for i, key in enumerate(keys[:2])
    ], retired_ids=[], now=1)

    def remap():
        new_model = {**model, "cat": {"genre:a": 1, "genre:b": 0}}
        generation = store.rec_model.content_generation()["generation"]
        assert store.rec_model.publish_content_generation(
            list(zip(keys, (v.tobytes() for v in vectors[:, ::-1]))), [],
            json.dumps(new_model), "rev", "new", generation)

    return entries, keys, remap


def test_ppr_keeps_generation_when_rebuild_happens_during_graph_read(store, monkeypatch):
    _entries, keys, remap = _seed_remappable_space(store)

    def build_transition(_store):
        remap()
        return keys, np.eye(len(keys)), {key: i for i, key in enumerate(keys)}

    monkeypatch.setattr(ppr, "build_transition", build_transition)
    rankings = ppr.mode_rankings(store, alpha=.85, iters=5, tol=0., depth=2)
    assert rankings[1][0] == keys[0]
    assert rankings[2][0] == keys[1]


def test_bundles_keep_ppr_in_candidate_generation_during_pool_rebuild(store, monkeypatch):
    _entries, keys, remap = _seed_remappable_space(store)

    def rebuild_while_ranking(*args, **kwargs):
        remap()
        return []

    monkeypatch.setattr(surfaces, "for_you", rebuild_while_ranking)
    for name in ("explore_for_you", "comfort_listening", "cold_candidates"):
        monkeypatch.setattr(surfaces, name, lambda *args, **kwargs: [])
    monkeypatch.setattr(ppr, "build_transition", lambda _store: (
        keys, np.eye(len(keys)), {key: i for i, key in enumerate(keys)}))

    payload = mode_surfaces.prepare_bundles(store, 100_000)
    assert payload["_ppr"]["1"][0] == keys[0]
    assert payload["_ppr"]["2"][0] == keys[1]


@pytest.mark.parametrize("classifier", [layers.now_mode_mix, layers.session_mode_mix])
def test_mode_classification_keeps_generation_during_rebuild(store, monkeypatch, classifier):
    _entries, keys, remap = _seed_remappable_space(store)
    identity = store.identities.upsert_identity("main", "cred", None, True)
    store.history.import_play_events(identity, [(keys[0], "a", 100_000), (keys[2], "c", 100_000)])
    rec_params.set_param(store, "now_min_events", 2)
    live_modes = taste_modes.live_modes

    def modes_then_rebuild(*args, **kwargs):
        modes = live_modes(*args, **kwargs)
        remap()
        return modes

    monkeypatch.setattr(taste_modes, "live_modes", modes_then_rebuild)
    shares, count, _modes = classifier(store, 100_000)
    assert count == 2
    assert shares == {1: pytest.approx(1.)}


def test_catalog_scores_keep_generation_when_rebuild_follows_taste_read(store, monkeypatch):
    entries, keys, remap = _seed_remappable_space(store)
    identity = store.identities.upsert_identity("main", "cred", None, True)
    playlist = store.playlists.upsert_playlist(identity, "P", "A context", 1, "h", 0.)
    store.playlists.set_playlist_tracks(playlist, [entries[0]])
    content_taste = surfaces.content_taste

    def taste_then_rebuild(*args, **kwargs):
        taste = content_taste(*args, **kwargs)
        remap()
        return taste

    monkeypatch.setattr(surfaces, "content_taste", taste_then_rebuild)
    scores = surfaces._content_only_scores(store, {}, 100_000)
    assert scores[keys[0]] > scores[keys[1]]
