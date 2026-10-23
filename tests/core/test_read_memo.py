import sqlite3

from yt_playlist.core.store import Store


def test_cache_hit_keeps_publication_dependencies_and_scopes_settings(store):
    store.settings.set_setting("input", "old")
    calls = []
    def calculate(reader):
        def prepare():
            calls.append(True)
            return reader.settings.get_setting("input")
        return reader.memoized_read("example", prepare)
    with store.read_snapshot() as snapshot:
        assert calculate(snapshot.store) == "old"
    store.settings.set_setting("unrelated", "new")
    with store.read_snapshot() as snapshot:
        assert calculate(snapshot.store) == "old"
        assert len(calls) == 1
        store.settings.set_setting("input", "new")
        assert not snapshot.publish(lambda: None)
    with store.read_snapshot() as snapshot:
        assert calculate(snapshot.store) == "new"
    assert len(calls) == 2


def test_nested_captures_retain_overlapping_dependencies(store):
    store.settings.set_setting("input", "old")
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        reader.settings.get_setting("input")
        reader.memoized_read("inner", lambda: reader.settings.get_setting("input"))
        assert reader.memoized_read("outer", lambda: reader.memoized_read(
            "inner", lambda: reader.settings.get_setting("input"))) == "old"
    store.settings.set_setting("input", "new")
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        assert reader.memoized_read("outer", lambda: reader.memoized_read(
            "inner", lambda: reader.settings.get_setting("input"))) == "new"


def test_external_commit_during_reader_pinning_cannot_reuse_old_result(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    store = Store(path)
    store.init_schema()
    store.settings.set_setting("input", "old")
    def calculate(reader):
        return reader.memoized_read("example", lambda: reader.settings.get_setting("input"))
    try:
        with store.read_snapshot() as snapshot:
            assert calculate(snapshot.store) == "old"
        original = Store._configure_connection
        written = []
        def configure(reader):
            original(reader)
            def on_pin(sql):
                if sql == "SELECT 1 FROM settings LIMIT 1" and not written:
                    with sqlite3.connect(path) as other:
                        other.execute("UPDATE settings SET value='new' WHERE key='input'")
                    written.append(True)
            reader.conn.set_trace_callback(on_pin)
        monkeypatch.setattr(Store, "_configure_connection", configure)
        with store.read_snapshot() as snapshot:
            assert calculate(snapshot.store) == "new"
        assert written
    finally:
        store.conn.close()
