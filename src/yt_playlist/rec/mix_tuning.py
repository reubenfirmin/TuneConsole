"""The Home mix controls are temporary, separate from learned taste."""
from contextlib import contextmanager

from yt_playlist.rec import rec_params

TTL_S = 4 * 3600
UPDATED_AT_SETTING = "home_mix_tuning_updated_at"


def remaining(store, now) -> float:
    raw = store.settings.get_setting(UPDATED_AT_SETTING)
    if raw is None:
        return 0.0
    return max(0.0, float(raw) + TTL_S - now)


def reset(store) -> None:
    """Clear manual choices, keeping play history, feedback and learned weights."""
    with store._lock:
        store.rec_model.clear_all_leans()
        store.rec_model.clear_hidden_facets()
        rec_params.reset_param(store, "breadth_bias")
        store.rec_surface.put_proposals("home_genre_request", {}, 0.0)
        store.settings.delete_setting("home_mix_vibe")
        store.settings.delete_setting(UPDATED_AT_SETTING)


def expire(store, now) -> bool:
    # Maintenance readers inherit an already-expired, committed tuning state.
    # They must never try to mutate their pinned read-only snapshot.
    if getattr(store, "_read_only", False):
        return False
    # Recheck under the shared lock: a simultaneous new adjustment must survive.
    with store._lock:
        if store.settings.get_setting(UPDATED_AT_SETTING) is None or remaining(store, now) > 0:
            return False
        reset(store)
        return True


@contextmanager
def adjusting(store, now):
    """Start a new four-hour window only when the user changes a control."""
    with store._lock:
        expire(store, now)
        yield
        store.settings.set_setting(UPDATED_AT_SETTING, str(now))
