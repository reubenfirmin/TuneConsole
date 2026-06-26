"""#49 capture-time play counting: record_history_plays records only NEW recently-played entries,
so the re-fetched (heavily-overlapping) window no longer inflates play counts."""
from yt_playlist.core.store import Store


def _store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _counts(s):
    return {r[0]: r[1] for r in s.conn.execute(
        "SELECT identity_key, COUNT(*) FROM history_items GROUP BY identity_key")}


def test_record_history_plays_dedups_lingering_window():
    s = _store()
    iid = s.upsert_identity("m", "c", None, True)
    win = ["a|x", "b|x", "c|x"]
    assert s.record_history_plays(iid, 1.0, win) == 3          # first window: all genuinely new
    assert s.record_history_plays(iid, 2.0, win) == 0          # same window re-synced: nothing new
    assert s.record_history_plays(iid, 3.0, ["d|x"] + win) == 1  # one new track on top of the window
    assert _counts(s) == {"a|x": 1, "b|x": 1, "c|x": 1, "d|x": 1}   # no lingering inflation


def test_record_history_plays_ignores_truncated_window():
    s = _store()
    iid = s.upsert_identity("m", "c", None, True)
    full = [f"t{i}|x" for i in range(10)]
    assert s.record_history_plays(iid, 1.0, full) == 10
    assert s.record_history_plays(iid, 2.0, ["t0|x"]) == 0     # truncated reply: ignored, not re-counted
    assert s.record_history_plays(iid, 3.0, full) == 0         # cache survived the hiccup -> no re-count
    assert all(c == 1 for c in _counts(s).values())


def test_record_history_plays_is_per_identity():
    s = _store()
    a = s.upsert_identity("a", "c", None, True)
    b = s.upsert_identity("b", "c", None, False)
    assert s.record_history_plays(a, 1.0, ["k|x"]) == 1
    assert s.record_history_plays(b, 1.0, ["k|x"]) == 1        # a different identity's window is separate
    assert _counts(s) == {"k|x": 2}
