"""Task 4 (#52): discover.gc_discovery reads the knob, holds tracks in live generated playlists, and
sweeps the pool."""
from types import SimpleNamespace

from yt_playlist.core.store import Store
from yt_playlist.rec import discover, rec_params


def test_gc_discovery_uses_knob_and_holds_generated(monkeypatch):
    s = Store(":memory:"); s.init_schema()
    day = 86400.0
    s.upsert_discovered_track("gen|x", "v", "A", "X", None, None, None, None, "r", 0.0)
    s.upsert_discovered_track("old|y", "v", "B", "Y", None, None, None, None, "r", 0.0)
    now = 100 * day
    s.mark_offered("track", ["gen|x", "old|y"], now - 40 * day)
    rec_params.set_param(s, "discovery_gc_days", 30)
    monkeypatch.setattr(type(s), "generated_track_keys", lambda self, *a, **k: {"gen|x"}, raising=False)
    ctx = SimpleNamespace(store=s, logger=SimpleNamespace(info=lambda *a, **k: None))
    out = discover.gc_discovery(ctx, now)
    keys = {r["identity_key"] for r in s.get_discovered_tracks()}
    assert keys == {"gen|x"} and out["tracks"] == 1
