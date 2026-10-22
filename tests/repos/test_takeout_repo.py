"""#61 bulk history import: day-model + play_events backfill, idempotent on re-import."""
from yt_playlist.core.store import Store

DAY = 86400.0


def _store():
    s = Store(":memory:"); s.init_schema()
    s.identities.upsert_identity("main", "bridge", None, True)
    return s


def test_import_plays_day_model_and_dedup():
    s = _store()
    plays = [("a|x", 100 * DAY + 3600), ("a|x", 100 * DAY + 50000),   # same UTC day: one row
             ("a|x", 101 * DAY + 3600), ("b|y", 100 * DAY + 7200)]
    assert s.history.import_plays(1, plays) == 3
    assert s.history.import_plays(1, plays) == 0                              # idempotent
    counts = s.history.listening_day_counts()
    assert counts == {"a|x": 2, "b|y": 1}
    assert s.history.event_play_counts()=={'a|x':3,'b|y':1}


def test_import_plays_coexists_with_live_recording():
    s = _store()
    s.history.record_history_plays(1, 100 * DAY + 50000, ["a|x"], source_timezone='UTC')
    assert s.history.import_plays(1, [("a|x", 100 * DAY + 3600)]) == 0        # same day: no double count


def test_import_play_events_bulk_and_idempotent():
    s = _store()
    rows = [("a|x", "v1", 100 * DAY + 3600.5), ("b|y", None, 100 * DAY + 7200.0)]
    assert s.history.import_play_events(1, rows) == 2
    assert s.history.import_play_events(1, rows) == 0
    evs = s.history.play_events_since(0)
    assert len(evs) == 2 and evs[0]["video_id"] == "v1"


def test_import_play_events_cross_format_near_duplicates_dedupe():
    # The HTML export floors timestamps to whole seconds; the JSON export carries milliseconds.
    # Importing one format and then the other must not double-count the same play (found live:
    # an HTML import followed by a JSON redo doubled 1062 events), so idempotency uses a small
    # window, not exact equality. No real song can be played twice within 2 seconds.
    s = _store()
    assert s.history.import_play_events(1, [("a|x", "v1", 100 * DAY + 3600.0)]) == 1     # HTML import
    assert s.history.import_play_events(1, [("a|x", "v1", 100 * DAY + 3600.774)]) == 0   # JSON redo
    assert s.history.import_play_events(1, [("a|x", "v1", 100 * DAY + 3700.0)]) == 1     # a real new play


def test_import_play_events_does_not_collide_with_live_rows():
    s = _store()
    s.history.record_play_event(1, "a|x", "v1", 100 * DAY + 3600.5)           # live row, same instant
    assert s.history.import_play_events(1, [("a|x", "v1", 100 * DAY + 3600.5)]) == 0
    assert s.history.import_play_events(1, [("a|x", "v1", 100 * DAY + 9999.0)]) == 1
