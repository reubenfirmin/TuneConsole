"""The Home mix controls are temporary, separate from learned taste."""
from contextlib import contextmanager
from dataclasses import dataclass
import json

from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.rec_model_policy import SESSION_IDLE_S, session_identity, steering_remaining
from yt_playlist.core.recommendation_requests import HOME_GENRE, HOME_VIBE
from yt_playlist.rec import rec_params

TTL_S = SESSION_IDLE_S
UPDATED_AT_SETTING = "home_mix_tuning_updated_at"
VIBE_UPDATED_AT_SETTING = 'home_mix_vibe_updated_at'

PUBLICATION_INPUTS = frozenset({
    ('taste_axes', 'group:mix_controls'),
    *(('settings', 'key:' + key) for key in (
        HOME_GENRE, HOME_VIBE,
        UPDATED_AT_SETTING, UPDATED_AT_SETTING + '_session',
        VIBE_UPDATED_AT_SETTING, VIBE_UPDATED_AT_SETTING + '_session',
        'home_mix_vibe',
        rec_params.SETTING_PREFIX + 'breadth_bias', rec_params.SETTING_PREFIX + 'erosion_view_cap')),
})


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


def _left(store, key, now, get_session=None):
    raw = store.settings.get_setting(key)
    if raw is None:
        return None
    from yt_playlist.library.listening_window import active_session, first_steering_session
    scope = json.loads(store.settings.get_setting(key + '_session') or 'null')
    session = active_session(store, now) if get_session is None else get_session()
    return steering_remaining(float(raw), scope, session, now,
                              lambda at, until: first_steering_session(store, at, until))


def _set_clock(store, key, now):
    from yt_playlist.library.listening_window import active_session
    store.settings.set_setting(key, str(now))
    session = active_session(store, now)
    store.settings.set_setting(key + '_session', json.dumps(session_identity(session)))
    return session


def clock_left(raw, now):
    return None if raw is None else max(0.0, float(raw) + TTL_S - now)


def expiry_values(ordinary, vibe, now):
    ordinary, vibe = clock_left(ordinary, now), clock_left(vibe, now)
    return {'ordinary': ordinary == 0, 'vibe': vibe == 0 or (vibe is None and ordinary == 0),
            'resume_genres': ordinary != 0, 'has_vibe_clock': vibe is not None}


def remaining(store, now, *, get_session=None) -> float:
    values = [_left(store, key, now, get_session) for key in (UPDATED_AT_SETTING, VIBE_UPDATED_AT_SETTING)]
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
            store.settings.delete_setting(VIBE_UPDATED_AT_SETTING + '_session')
        store.settings.delete_setting(UPDATED_AT_SETTING)
        store.settings.delete_setting(UPDATED_AT_SETTING + '_session')


def expire(store, now, *, reader=None) -> bool:
    # Maintenance readers inherit an already-expired, committed tuning state.
    # They must never try to mutate their pinned read-only snapshot.
    if getattr(store, "_read_only", False) or getattr(store, 'activity_paused', False):
        return False
    if reader is not None and not _expiry_due(reader, now):
        return False
    if not store._lock.acquire(blocking=False):
        # A committed, unexpired state needs no writer. Only an expired clock
        # warrants waiting for maintenance, then rechecking a newer adjustment.
        with store.read_view() as reader:
            if not _expiry_due(reader, now):
                return False
        store._lock.acquire()
    try:
        changed = False
        # Older saved vibes shared the ordinary deadline. Preserve that deadline
        # until the first explicit vibe change gives them their own clock.
        ordinary, vibe = (_left(store, key, now) for key in (UPDATED_AT_SETTING, VIBE_UPDATED_AT_SETTING))
        decision = {'ordinary': ordinary == 0, 'vibe': vibe == 0 or (vibe is None and ordinary == 0),
                    'resume_genres': ordinary != 0, 'has_vibe_clock': vibe is not None}
        if not decision['ordinary'] and not decision['vibe']:
            return False
        with atomic_write(store):
            if decision['vibe']:
                changed = bool(store.settings.get_setting('home_mix_vibe')) or decision['has_vibe_clock']
                store.settings.delete_setting('home_mix_vibe')
                store.settings.delete_setting(VIBE_UPDATED_AT_SETTING)
                store.settings.delete_setting(VIBE_UPDATED_AT_SETTING + '_session')
                if decision['resume_genres']:
                    _resume_genres(store, now)
            if decision['ordinary']:
                reset(store, keep_vibe=True)
                changed = True
        return changed
    finally:
        store._lock.release()


def _expiry_due(reader, now):
    return any(_left(reader, key, now) == 0 for key in (UPDATED_AT_SETTING, VIBE_UPDATED_AT_SETTING))


@contextmanager
def adjusting(store, now):
    """Bind a deliberate adjustment to playback, or hold it for the next session."""
    with store._lock:
        expire(store, now)
        with atomic_write(store):
            yield
            _set_clock(store, UPDATED_AT_SETTING, now)


@dataclass
class VibeAdjustment:
    remaining: float = 0.0


@contextmanager
def adjusting_vibe(store, now):
    """Bind explicit vibe changes to playback independently of other controls."""
    adjustment = VibeAdjustment()
    with store._lock:
        expire(store, now)
        # Readers must observe the preset, retained genre request and its
        # deadline together. Commit the deliberate adjustment once, including
        # when repository helpers normally finish their individual writes.
        with atomic_write(store):
            yield adjustment
            if store.settings.get_setting('home_mix_vibe'):
                _hold_genres(store, now)
                session = _set_clock(store, VIBE_UPDATED_AT_SETTING, now)
                adjustment.remaining = remaining(store, now, get_session=lambda: session)
            else:
                _resume_genres(store, now)
                store.settings.delete_setting(VIBE_UPDATED_AT_SETTING)
                store.settings.delete_setting(VIBE_UPDATED_AT_SETTING + '_session')
                adjustment.remaining = remaining(store, now)
