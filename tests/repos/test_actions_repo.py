"""DAO suite for ActionRepo (the undoable action log)."""


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


def test_facade_delegates(store):
    aid = store.record_action("merge", "{}", "[]", "executed", "{}", 1.0)   # legacy store.x() call site
    assert store.get_action(aid).kind == "merge"
