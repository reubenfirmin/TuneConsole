"""The Home mix controls are temporary, separate from learned taste."""
from contextlib import contextmanager

from yt_playlist.core.db_writes import atomic_write
from yt_playlist.rec import rec_params

TTL_S = 4 * 3600
UPDATED_AT_SETTING = "home_mix_tuning_updated_at"
VIBE_UPDATED_AT_SETTING = 'home_mix_vibe_updated_at'
VIBE_REQUEST_SETTING = 'home_mix_vibe_request'


def card_epoch(store):
    cap = max(1, rec_params.get_param(store, 'erosion_view_cap'))
    return max(0, store.rec_ledger.card_views('cards') - 1) // cap


def lean_for_target(current_weight, target):
    """Pure: the temporary lean so permanent * lean == target, clamped to the genre lean range.

    The fingerprint bar shows the EFFECTIVE multiplier (permanent x lean); storing the lean
    rather than the target keeps long-term taste edits (the Taste Model page) untouched.
    """
    lean = target / current_weight if current_weight > 0 else target
    return max(rec_params.GENRE_MIN, min(rec_params.GENRE_MAX, lean))


def genre_active(store, request):
    return (request.get('epoch') == card_epoch(store)
            or (bool(store.settings.get_setting('home_mix_vibe'))
                and (request.get('held_by_vibe') or request.get('suspended_by_vibe', False))))


def _hold_genres(store, now):
    request = store.rec_proposals.get_proposals('home_genre_request') or {}
    if request and genre_active(store, request) and not request.get('held_by_vibe'):
        request.pop('suspended_by_vibe', None)
        store.rec_proposals.put_proposals('home_genre_request', {**request, 'held_by_vibe': True}, now)


def _resume_genres(store, now):
    request = store.rec_proposals.get_proposals('home_genre_request') or {}
    held = request.pop('held_by_vibe', False)
    legacy = request.pop('suspended_by_vibe', False)
    if held or legacy:
        store.rec_proposals.put_proposals('home_genre_request', {**request, 'epoch': card_epoch(store)}, now)


def _left(store, key, now):
    raw = store.settings.get_setting(key)
    return None if raw is None else max(0.0, float(raw) + TTL_S - now)


def remaining(store, now) -> float:
    values = [_left(store, key, now) for key in (UPDATED_AT_SETTING, VIBE_UPDATED_AT_SETTING)]
    return min((value for value in values if value is not None), default=0.0)


def reset(store, *, keep_vibe=False) -> None:
    """Clear manual choices, keeping play history, feedback and learned weights."""
    with atomic_write(store):
        store.rec_model.clear_all_leans()
        store.rec_model.clear_hidden_facets()
        rec_params.reset_param(store, "breadth_bias")
        store.rec_proposals.put_proposals("home_genre_request", {}, 0.0)
        if not keep_vibe:
            store.settings.delete_setting("home_mix_vibe")
            store.settings.delete_setting(VIBE_UPDATED_AT_SETTING)
        store.settings.delete_setting(VIBE_REQUEST_SETTING)
        store.settings.delete_setting(UPDATED_AT_SETTING)


def expire(store, now) -> bool:
    # Maintenance readers inherit an already-expired, committed tuning state.
    # They must never try to mutate their pinned read-only snapshot.
    if getattr(store, "_read_only", False) or getattr(store, 'activity_paused', False):
        return False
    if not store._lock.acquire(blocking=False):
        # A committed, unexpired state needs no writer. Only an expired clock
        # warrants waiting for maintenance, then rechecking a newer adjustment.
        with store.read_view() as reader:
            if (_left(reader, UPDATED_AT_SETTING, now) != 0
                    and _left(reader, VIBE_UPDATED_AT_SETTING, now) != 0):
                return False
        store._lock.acquire()
    try:
        changed = False
        # Older saved vibes shared the ordinary deadline. Preserve that deadline
        # until the first explicit vibe change gives them their own clock.
        ordinary = _left(store, UPDATED_AT_SETTING, now)
        vibe = _left(store, VIBE_UPDATED_AT_SETTING, now)
        if ordinary != 0 and vibe != 0:
            return False
        with atomic_write(store):
            if vibe == 0 or (vibe is None and ordinary == 0):
                changed = bool(store.settings.get_setting('home_mix_vibe')) or vibe is not None
                store.settings.delete_setting('home_mix_vibe')
                store.settings.delete_setting(VIBE_UPDATED_AT_SETTING)
                store.settings.delete_setting(VIBE_REQUEST_SETTING)
                if ordinary != 0:
                    _resume_genres(store, now)
            if ordinary == 0:
                reset(store, keep_vibe=True)
                changed = True
        return changed
    finally:
        store._lock.release()


@contextmanager
def adjusting(store, now):
    """Start a new four-hour window only when the user changes a control."""
    with store._lock:
        expire(store, now)
        yield
        store.settings.delete_setting(VIBE_REQUEST_SETTING)
        store.settings.set_setting(UPDATED_AT_SETTING, str(now))


@contextmanager
def adjusting_vibe(store, now):
    """Vibe changes neither reset nor extend the other controls' four-hour window."""
    with store._lock:
        expire(store, now)
        # Readers must observe the preset, retained genre request and its
        # deadline together. Commit the deliberate adjustment once, including
        # when repository helpers normally finish their individual writes.
        with atomic_write(store):
            yield
            if store.settings.get_setting('home_mix_vibe'):
                _hold_genres(store, now)
                store.settings.set_setting(VIBE_UPDATED_AT_SETTING, str(now))
            else:
                _resume_genres(store, now)
                store.settings.delete_setting(VIBE_UPDATED_AT_SETTING)
