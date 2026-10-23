import json
import logging
import sqlite3
from types import SimpleNamespace

import numpy as np
import pytest

from yt_playlist.core.activity import Activity
from yt_playlist.core.store import Store
from yt_playlist.rec import home_cards, measurement, rec_params
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.repos.rec_model import RecModelRepo


def _cards():
    return [{"lane": f"lane-{i}", "tracks": [{"key": "song|artist", "score": i}],
             "ranker": "ppr", "experiment": {"share": .5, "cluster": f"cluster-{i}"},
             "counterfactual": {"ranker": "lane", "tracks": [{"key": "song|artist", "score": -i}]}}
            for i in range(4)]


def test_home_response_shares_policy_and_unchanged_models_skip_full_reads(store, monkeypatch):
    reads = []
    for method in ["get_rec_vectors", "get_rec_content_vectors", "get_rec_discovered_content_vectors",
                   "get_rec_artist_vectors", "get_rec_artist_content_vectors"]:
        original = getattr(RecModelRepo, method)
        def counted(repo, original=original, method=method):
            reads.append(method)
            return original(repo)
        monkeypatch.setattr(RecModelRepo, method, counted)
    policies, original = [], measurement.serving_policy
    def policy(*args, **kwargs):
        policies.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(measurement, "serving_policy", policy)
    home_cards._record_served_cards(store, _cards(), 100)
    assert len(policies) == 1
    assert all(1 <= reads.count(method) <= 2 for method in set(reads))
    assert len(set(reads)) == 5
    reads.clear()
    home_cards._record_served_cards(store, _cards(), 101)
    assert len(policies) == 2
    assert reads == []  # Analytics writes and a new response time do not invalidate model inputs.
    rows = store.conn.execute("SELECT model_version,provenance FROM rec_served_impressions").fetchall()
    assert len(rows) == 16
    configs = [store.rec_surface.served_recommendation(r[0])["configuration"] for r in
               store.conn.execute("SELECT id FROM rec_served_impressions")]
    assert {c["ranker"] for c in configs} == {"ppr", "lane"}
    assert {json.loads(r["provenance"])["policy_kind"] for r in rows} == {"live", "counterfactual"}


@pytest.mark.parametrize("suffix,field", [
    ("vectors", "collaborative"), ("content_vectors", "content"),
    ("discovered_content_vectors", "discovered_content"), ("artist_vectors", "artists"),
    ("artist_content_vectors", "artist_content")])
def test_each_vector_publication_invalidates_model_identity(store, suffix, field):
    replace = getattr(store.rec_model, "replace_rec_" + suffix)
    replace([("song|artist", np.asarray([1., 0.], dtype=np.float32).tobytes())])
    before = measurement.serving_policy(store, 100)[1]["models"][field]
    replace([("song|artist", np.asarray([0., 1.], dtype=np.float32).tobytes())])
    after = measurement.serving_policy(store, 100)[1]["models"][field]
    assert before != after


def test_catalog_inputs_invalidate_baskets_and_content_space_settings(store):
    a = store.tracks.upsert_track("a", "A", "Artist", None, 200)
    b = store.tracks.upsert_track("b", "B", "Artist", None, 200)
    before = measurement.serving_policy(store, 100)[1]["models"]
    store.tracks.set_track_genre(a, "Rock")
    store.tracks.set_track_genre(b, "Rock")
    after = measurement.serving_policy(store, 100)[1]["models"]
    assert before["baskets"] != after["baskets"]
    store.settings.set_setting("rec_content_model", '{"cat":{},"ncat":0,"cont":[]}')
    assert measurement.serving_policy(store, 100)[1]["models"]["content_space"] != after["content_space"]


def test_external_vector_write_invalidates_cached_identity(tmp_path):
    store = Store(tmp_path / "state.db")
    store.init_schema()
    try:
        before = measurement.serving_policy(store, 100)[1]["models"]["collaborative"]
        with sqlite3.connect(tmp_path / "state.db") as other:
            other.execute("INSERT INTO rec_vectors VALUES (?,?)",
                          ("song|artist", np.asarray([1., 0.], dtype=np.float32).tobytes()))
        assert measurement.serving_policy(store, 100)[1]["models"]["collaborative"] != before
    finally:
        store.conn.close()


def test_mode_centroid_publication_invalidates_model_identity(store):
    mode = {"mode_id": 1, "label": "Mode", "families": [], "centroid": [1., 0.], "size": 2, "rep_keys": []}
    store.modes.replace_modes([mode], [], 1)
    before = measurement.serving_policy(store, 100)[1]["models"]["modes"]
    store.modes.replace_modes([{**mode, "centroid": [0., 1.]}], [], 2)
    assert measurement.serving_policy(store, 100)[1]["models"]["modes"] != before


def test_new_listening_evidence_invalidates_basket_identity(store):
    identity = store.identities.upsert_identity("main", "ref", None, True)
    keys = []
    for i in range(2):
        tid = store.tracks.upsert_track(f"v{i}", f"Song {i}", f"Artist {i}", None, 200)
        keys.append(store.tracks.catalog_subject(tid)["identity_key"])
    before = measurement.serving_policy(store, 100)[1]["models"]["baskets"]
    store.history.import_listening_events(identity, [(key, None, 1000) for key in keys], source_timezone="UTC")
    assert measurement.serving_policy(store, 100)[1]["models"]["baskets"] != before


def test_failed_card_does_not_drop_remaining_response_measurements(store, monkeypatch):
    original = store.rec_surface.record_served_impressions
    calls = []
    def fail_first(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("instrumentation failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(store.rec_surface, "record_served_impressions", fail_first)
    home_cards._record_served_cards(store, _cards(), 100)
    assert len(calls) == 8
    assert store.conn.execute("SELECT COUNT(*) FROM rec_served_impressions").fetchone()[0] == 7


def test_batched_queue_preserves_offer_bound_and_offer_time_attribution(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    store.init_schema()
    worker = RecWorker(SimpleNamespace(store=store, work=Activity(), logger=logging.getLogger(__name__),
                                       now_fn=lambda: 100))
    monkeypatch.setattr(worker, "_start_loop", lambda: None)
    try:
        for _ in range(9):
            home_cards._record_served_cards(store, _cards(), 100)
        assert len(worker.jobs.measurements) == 8
        assert sum(offer.work_units for offer in worker.jobs.measurements) == 64
        readers = [offer.snapshot.store.conn for offer in worker.jobs.measurements]
        rec_params.set_param(store, "axis_weight_cap", 2)
        store.rec_model.replace_rec_vectors([("song|artist", np.asarray([1., 0.], dtype=np.float32).tobytes())])
        worker._flush_measurements()
        assert store.conn.execute("SELECT COUNT(*) FROM rec_served_impressions").fetchone()[0] == 64
        for row in store.conn.execute("SELECT id FROM rec_served_impressions"):
            config = store.rec_surface.served_recommendation(row[0])["configuration"]
            assert config["params"]["axis_weight_cap"] == 1.4
            assert config["models"]["collaborative"] == measurement._vector_identity([])
        for reader in readers:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                reader.execute("SELECT 1")
    finally:
        worker.shutdown()
        store.conn.close()
