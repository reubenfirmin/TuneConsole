"""#91 raw player/curation event stream: append-only, kind-tagged, identity-attributed."""
from yt_playlist.core.store import Store


def _store():
    s = Store(":memory:"); s.init_schema()
    s.upsert_identity("main", "bridge", None, True)
    return s


def test_record_and_read_back():
    s = _store()
    rid = s.record_player_event(1, "track_exit", "v1", 19.5, 400.0, "PL9", None, 1000.0)
    assert isinstance(rid, int)
    evs = s.player_events_since(0)
    assert len(evs) == 1
    e = evs[0]
    assert e["kind"] == "track_exit" and e["video_id"] == "v1"
    assert e["position"] == 19.5 and e["duration"] == 400.0
    assert e["playlist_ytm_id"] == "PL9" and e["payload"] is None and e["at"] == 1000.0


def test_payload_roundtrip_and_kind_filter():
    s = _store()
    s.record_player_event(1, "state", "v1", 10.0, 400.0, None, '{"state": "paused"}', 1000.0)
    s.record_player_event(1, "rate", "v2", None, None, None, '{"action": "like"}', 2000.0)
    assert [e["kind"] for e in s.player_events_since(0)] == ["state", "rate"]
    rates = s.player_events_since(0, kind="rate")
    assert len(rates) == 1 and rates[0]["payload"] == '{"action": "like"}'


def test_since_filter_and_ordering():
    s = _store()
    s.record_player_event(1, "tick", "v1", 30.0, 400.0, None, None, 1000.0)
    s.record_player_event(1, "tick", "v1", 60.0, 400.0, None, None, 2000.0)
    evs = s.player_events_since(1500.0)
    assert len(evs) == 1 and evs[0]["position"] == 60.0
