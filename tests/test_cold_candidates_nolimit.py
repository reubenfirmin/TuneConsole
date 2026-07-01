import inspect
from yt_playlist.rec import surfaces


def test_cold_candidates_default_limit_is_none():
    # The worker now ranks the whole pool and buckets per mode, so the default must not truncate.
    sig = inspect.signature(surfaces.cold_candidates)
    assert sig.parameters["limit"].default is None
