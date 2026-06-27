"""Task 3 (#52): the discovery_gc_days tuning knob."""
from yt_playlist.core.store import Store
from yt_playlist.rec import rec_params


def test_discovery_gc_days_default_and_clamp():
    s = Store(":memory:"); s.init_schema()
    assert rec_params.get_param(s, "discovery_gc_days") == 30
    rec_params.set_param(s, "discovery_gc_days", 9999)
    assert rec_params.get_param(s, "discovery_gc_days") == 365
    assert rec_params.PARAMS_BY_NAME["discovery_gc_days"].group == "discovery"
    assert rec_params.PARAMS_BY_NAME["discovery_gc_days"].integer is True
