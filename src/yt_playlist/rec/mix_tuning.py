"""The Home mix controls are temporary, separate from learned taste."""
from contextlib import contextmanager

from yt_playlist.rec import rec_params

TTL_S = 4 * 3600
UPDATED_AT_SETTING = "home_mix_tuning_updated_at"


def remaining(store, now) -> float:
    raw = store.get_setting(UPDATED_AT_SETTING)
    if raw is None:
        return 0.0
    return max(0.0, float(raw) + TTL_S - now)


def reset(store) -> None:
    """Clear manual choices, keeping play history, feedback and learned weights."""
    with store._lock:
        store.clear_all_leans()
        store.clear_hidden_facets()
        rec_params.reset_param(store, "breadth_bias")
        store.put_proposals("home_genre_request", {}, 0.0)
        store.delete_setting(UPDATED_AT_SETTING)


def expire(store, now) -> bool:
    # Recheck under the shared lock: a simultaneous new adjustment must survive.
    with store._lock:
        if store.get_setting(UPDATED_AT_SETTING) is None or remaining(store, now) > 0:
            return False
        reset(store)
        return True


@contextmanager
def adjusting(store, now):
    """Start a new four-hour window only when the user changes a control."""
    with store._lock:
        expire(store, now)
        yield
        store.set_setting(UPDATED_AT_SETTING, str(now))
