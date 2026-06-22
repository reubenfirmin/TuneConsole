"""Persistent mood events: no wall-clock purge, count-capped at MOOD_EVENT_CAP."""


def test_recent_mood_events_persist_newest_first(store):
    store.record_mood(["a|x"], 1, now=1000.0)
    store.record_mood(["b|x"], -1, now=2000.0)
    store.record_mood(["c|x"], 1, now=1000.0 - 90 * 86400)     # 90 days "old"
    ev = store.recent_mood_events()
    assert [e[0] for e in ev[:2]] == [2000.0, 1000.0]          # newest-first
    assert any(e[2] == ["c|x"] for e in ev)                    # NOT purged by time


def test_record_mood_trims_to_cap(store, monkeypatch):
    from yt_playlist.rec import rec_params
    monkeypatch.setattr(rec_params, "MOOD_EVENT_CAP", 3)
    for i in range(5):
        store.record_mood([f"k{i}|x"], 1, now=1000.0 + i)
    ev = store.recent_mood_events(limit=100)
    assert [e[2][0] for e in ev] == ["k4|x", "k3|x", "k2|x"]
