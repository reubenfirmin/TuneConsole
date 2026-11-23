"""Temporary, explicit sound preferences for Home mixes, applied before track selection."""
import math
from contextlib import contextmanager
from contextvars import ContextVar

from yt_playlist.rec import mix_tuning, musical_character
from yt_playlist.rec.ordering import _field
from yt_playlist.rec.vibe_presets import (
    PRESETS as PRESETS,
    axis_fit as axis_fit,
    heuristic_affinity as heuristic_affinity,
)

SETTING = "home_mix_vibe"
VERSION = 'musical-vibes-7'
FIT_STEP = .04
_NATURAL = ContextVar('onboarding_natural_mix', default=False)
_AUTHORIZED = ContextVar('authorized_vibe_request', default=None)
_ACCESS = ContextVar('vibe_access_policy', default=None)
_access = None


def configure_access(check):
    """Supply the journey policy from the application or worker composition root.

    Ranking is also used by the journey's natural-menu readiness checks. It
    cannot import that higher-level service without making preparation depend
    on its own serving gate.
    """
    global _access
    _access = check


@contextmanager
def access_policy(check):
    """Use the request's live session when checking its remembered selection."""
    token = _ACCESS.set(check)
    try:
        yield
    finally:
        _ACCESS.reset(token)


@contextmanager
def natural():
    """Inspect ordinary preparation without a remembered, possibly locked Vibe."""
    token = _NATURAL.set(True)
    try:
        yield
    finally:
        _NATURAL.reset(token)


@contextmanager
def authorized(store, preset=None):
    """Reuse this operation's completed journey guard while rendering its result."""
    if preset is not None and preset != '' and preset not in PRESETS:
        raise ValueError('Unknown authorized Vibe')
    token = _AUTHORIZED.set((getattr(store, '_evidence_source', store), preset))
    try:
        yield
    finally:
        _AUTHORIZED.reset(token)


def current(store, now):
    if _NATURAL.get():
        return ''
    authorized = _AUTHORIZED.get()
    same_operation = authorized is not None and authorized[0] is getattr(store, '_evidence_source', store)
    if same_operation and authorized[1] is not None:
        return authorized[1]
    mix_tuning.expire(store, now)
    value = store.settings.get_setting(SETTING)
    if value not in PRESETS:
        return ''
    if same_operation:
        return value
    check = _ACCESS.get() or _access
    if check is None:
        raise RuntimeError('Vibe access policy has not been configured')
    return value if check(store, now) else ''


def affinity(item, preset, profiles=None):
    """Rank with a prepared Vibe score or the neutral midpoint."""
    profile = _field(item, 'musical') or (profiles or {}).get(_field(item, 'key'))
    fit = (profile or {}).get('vibes', {}).get('fits', {}).get(preset, {})
    resolved = fit.get('score')
    if (fit.get('source') not in ('provider_measurement', 'heuristic')
            and type(resolved) in (int, float) and math.isfinite(resolved) and 0 <= resolved <= 1):
        return resolved
    return .5


def affinities(store, keys, preset):
    current = store.vibe_predictions.fit_scores(keys, preset)
    return {key: score_or_neutral(current.get(key)) for key in keys}


def score_or_neutral(score):
    return score if type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1 else .5


def taste_score(item):
    """Fixed bounded scale for actual scores, independent of pool size or position.

    This monotonic link is an engineering policy, not empirical calibration.
    Missing scores remain unknown/neutral and never inherit their list position.
    """
    score = _field(item, 'score')
    if type(score) not in (int, float) or not math.isfinite(score):
        return .5
    return .5 + .5 * score / (1 + abs(score))


def order_key(item, preset, profiles=None):
    fit = affinity(item, preset, profiles)
    return (-round((fit - .5) / FIT_STEP), -taste_score(item), -fit)


class Ranker:
    def __init__(self, preset, profiles=None):
        self.preset = preset
        self.profiles = profiles or {}

    def affinity(self, item):
        return affinity(item, self.preset, self.profiles) if self.preset else .5

    def __call__(self, items):
        return sorted(items, key=lambda item: order_key(item, self.preset, self.profiles)) if self.preset else items


class ProfileRanker:
    """Acquire only keys visited by bounded menu consumers before detached ranking."""
    def __init__(self, preset, load):
        self.preset, self.load = preset, load
        self.profiles, self.loaded = {}, set()

    def _ensure(self, items):
        keys = {_field(item, 'key') for item in items
                if _field(item, 'key') and not (isinstance(item, dict) and 'musical' in item)} - self.loaded
        if keys:
            self.profiles.update(self.load(sorted(keys)))
        self.loaded.update(keys)

    def affinity(self, item):
        if not self.preset:
            return .5
        self._ensure([item])
        return Ranker(self.preset, self.profiles).affinity(item)

    def __call__(self, items):
        if not self.preset:
            return items
        self._ensure(items)
        return Ranker(self.preset, self.profiles)(items)


def ranker(store, now):
    """Capture metadata once for a whole menu; preserve taste order when sound evidence ties."""
    return ProfileRanker(current(store, now), lambda keys: musical_character.load(store, keys))


def rank(store, now, items):
    return ranker(store, now)(items)
