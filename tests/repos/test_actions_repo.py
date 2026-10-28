"""DAO suite for ActionRepo (the undoable action log)."""
import json


def test_record_get_update(store):
    aid = store.actions.record_action("merge", "{}", "[]", "planned", "{}", 1.0)
    a = store.actions.get_action(aid)
    assert a.kind == "merge" and a.status == "planned" and a.executed_at is None
    store.actions.update_action(aid, "executed", 2.0)
    assert store.actions.get_action(aid).status == "executed"
    store.actions.update_action(aid, "undone", 3.0, undo_json='{"x":1}')
    a = store.actions.get_action(aid)
    assert a.status == "undone" and a.undo_json == '{"x":1}'


def test_get_actions_newest_first(store):
    a1 = store.actions.record_action("merge", "{}", "[]", "executed", "{}", 1.0)
    a2 = store.actions.record_action("move", "{}", "[]", "executed", "{}", 2.0)
    assert [a.id for a in store.actions.get_actions()] == [a2, a1]   # ORDER BY id DESC


def test_get_action_missing_returns_none(store):
    assert store.actions.get_action(999) is None


def test_store_binding(store):
    aid = store.actions.record_action("merge", "{}", "[]", "executed", "{}", 1.0)
    assert store.actions.get_action(aid).kind == "merge"


def test_pending_deletion_matches_account_and_playlist_in_the_same_entry(store):
    def record(entries, status="delete_pending"):
        return store.actions.record_action("apply_merge", "{}", "{}", status,
                                           json.dumps({"deletions": entries}), 1)

    wanted = {"identity": 7, "ytm": "SOURCE"}
    older = record([wanted])
    newer = record([{"identity": 7, "ytm": "OTHER"}, wanted])
    record([{"identity": 8, "ytm": "SOURCE"}, {"identity": 7, "ytm": "OTHER"}])
    for status in ("executed", "delete_failed", "undoing", "undone"):
        record([wanted], status)
    record([])

    assert store.actions.get_pending_deletion(7, "SOURCE").id == newer
    assert store.actions.get_pending_deletion(9, "SOURCE") is None
    assert store.actions.get_pending_deletion(7, "MISSING") is None
    store.actions.update_action(newer, "executed", 2)
    assert store.actions.get_pending_deletion(7, "SOURCE").id == older
    store.actions.update_action(older, "executed", 3)
    assert store.actions.get_pending_deletion(7, "SOURCE") is None
    fresh = record([wanted])
    assert store.actions.get_pending_deletion(7, "SOURCE").id == fresh


def test_pending_deletion_excludes_completed_payloads_and_only_materializes_match(store, monkeypatch):
    from yt_playlist.repos import actions

    with store.conn:
        store.conn.executemany(
            "INSERT INTO actions(kind,status,undo_json) VALUES ('merge','executed',?)",
            [("unrelated historical payload" * 100,)] * 500)
    undo = json.dumps({"deletions": [{"identity": 7, "ytm": "SOURCE"}]})
    expected = store.actions.record_action("delete_playlist", "{}", "{}", "delete_pending", undo, 1)
    store.actions.record_action("delete_playlist", "{}", "{}", "delete_pending", "{}", 2)
    constructed = []
    original = actions.Action

    def action(*args):
        constructed.append(args[0])
        return original(*args)

    monkeypatch.setattr(actions, "Action", action)
    queries = []
    store.conn.set_trace_callback(queries.append)
    try:
        assert store.actions.get_pending_deletion(7, "SOURCE").id == expected
    finally:
        store.conn.set_trace_callback(None)
    assert constructed == [expected]
    query, = [sql for sql in queries if sql.startswith("SELECT a.* FROM actions")]
    plan = store.conn.execute("EXPLAIN QUERY PLAN " + query).fetchall()
    assert any("ix_actions_delete_pending" in row[3] for row in plan)


def test_failed_deletion_lookup_matches_one_entry_and_preserves_manual_pending_lookup(store):
    def record(entries, status="delete_failed"):
        return store.actions.record_action("gc_generated", "{}", "{}", status,
                                           json.dumps({"deletions": entries}), 1)

    wanted = {"identity": 7, "ytm": "SOURCE", "phase": "failed"}
    older = record([wanted])
    newest = record([wanted])
    record([{"identity": 8, "ytm": "SOURCE"}, {"identity": 7, "ytm": "OTHER"}])
    for status in ("executed", "undone", "undo_failed"):
        record([wanted], status)
    assert store.actions.get_failed_deletion(7, "SOURCE").id == newest
    assert store.actions.get_pending_deletion(7, "SOURCE") is None
    assert store.actions.get_failed_deletion(9, "SOURCE") is None
    store.actions.update_action(newest, "undone", 2)
    assert store.actions.get_failed_deletion(7, "SOURCE").id == older
