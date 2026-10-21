"""Content publication preserves complete generations under failures and competing encoders."""
import json
import sqlite3
import threading

import pytest

from yt_playlist.core.store import Store
from yt_playlist.rec import embed


def _seed(store):
    tid = store.upsert_track("owned", "Owned", "Artist", None, None)
    store.set_track_genre(tid, "Techno")
    store.upsert_discovered_track("cold|artist", "cold", "Cold", "Artist", "", None,
                                  "Techno", None, "source", 0)
    embed.build_content_and_store(store)
    return tid


def test_failed_publication_rolls_back_all_artifacts_even_after_later_commit(store):
    _seed(store)
    before = store.content_generation()
    store.conn.execute("CREATE TRIGGER reject_content BEFORE INSERT ON rec_discovered_content_vectors "
                       "BEGIN SELECT RAISE(ABORT,'injected write failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected write failure"):
        store.publish_content_generation([("new|artist", b"new")], [("bad|artist", b"bad")],
            "new model", "new revision", "new inputs", before["generation"])
    store.set_setting("unrelated", "committed")
    assert store.content_generation() == before


def test_discovery_encoder_retries_against_competing_library_generation(store, monkeypatch):
    tid = _seed(store)
    encode = embed._encode_discovered
    switched = False

    def competing_encode(rows, model):
        nonlocal switched
        if not switched:
            switched = True
            store.set_track_genre(tid, "Folk")
            embed.build_content_and_store(store)
        return encode(rows, model)

    monkeypatch.setattr(embed, "_encode_discovered", competing_encode)
    assert embed.build_discovered_content_vectors(store) == 0
    snapshot = store.content_generation()
    assert "fam:folk-country" in json.loads(snapshot["model"])["cat"]
    assert snapshot["library"] and snapshot["discovered"] == []


def test_reader_sees_old_then_new_complete_generation_during_publication(tmp_path):
    writer = Store(tmp_path / "generation.db")
    writer.init_schema()
    _seed(writer)
    before = writer.content_generation()
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
            assert writer.publish_content_generation([("new|artist", b"new library")],
                [("new cold|artist", b"new discovery")], "new model", "revision", "inputs",
                before["generation"])
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        assert writing.wait(5)
        assert reader.content_generation() == before
    finally:
        release.set()
        thread.join(5)
    try:
        assert not thread.is_alive() and not errors
        after = reader.content_generation()
        assert after["generation"] != before["generation"]
        assert (after["model"], after["revision"], after["inputs"]) == ("new model", "revision", "inputs")
        assert after["library"] == [("new|artist", b"new library")]
        assert after["discovered"] == [("new cold|artist", b"new discovery")]
    finally:
        writer.conn.close()
        reader.conn.close()
