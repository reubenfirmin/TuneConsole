"""Playback continuity, local listening days and confidence-gated fast taste readings."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import json

import numpy as np

from yt_playlist.core.listening_calendar import zone
from yt_playlist.core.rec_model_policy import SESSION_IDLE_S
from yt_playlist.rec.transient_math import decay_weight

POLICY_VERSION = 2


@dataclass(frozen=True)
class PlaybackSession:
    account: int | None
    started_at: float
    last_at: float

    @property
    def expires_at(self):
        return self.last_at + SESSION_IDLE_S

    @property
    def episode(self):
        return f'{self.account}:{self.started_at}'


def playback_order(row):
    return row['at'], row.get('kind') == 'bye'


def first_session(activity, deadline):
    """Find the first session started before pre-play steering would expire."""
    sessions = {}
    for row in sorted(activity, key=playback_order):
        if row['at'] >= deadline:
            break
        if (session := advance_session(sessions, row)) is not None:
            return session
    return None


def playback_sessions(activity, now):
    """Reconstruct the audible sessions still active at the supplied time."""
    sessions = {}
    for row in sorted(activity, key=playback_order):
        if row['at'] > now:
            break
        advance_session(sessions, row)
    return [session for session in sessions.values() if now < session.expires_at]


def active_session(activity, now):
    return max(playback_sessions(activity, now), key=lambda session: session.last_at, default=None)


def advance_session(sessions, row):
    account, at, kind = row.get('account_id'), row['at'], row.get('kind')
    if kind == 'bye':
        sessions.pop(account, None)
        return None
    if kind is not None:
        try:
            facts = json.loads(row.get('payload') or '{}')
        except (TypeError, ValueError):
            return None
        if (not isinstance(facts, dict) or facts.get('paused') or facts.get('muted')
                or facts.get('tabMuted') or facts.get('volume') == 0
                or (kind == 'state' and facts.get('state') != 'playing')):
            return None
    previous = sessions.get(account)
    started = previous.started_at if previous and at < previous.expires_at else at
    sessions[account] = PlaybackSession(account, started, at)
    return sessions[account]


def calendar_window(now, timezone):
    local = datetime.fromtimestamp(now, zone(timezone))
    start = datetime.combine(local.date(), time.min, local.tzinfo).timestamp()
    end = datetime.combine(local.date() + timedelta(days=1), time.min, local.tzinfo).timestamp()
    return start, end


@dataclass(frozen=True)
class ListeningWindow:
    now: float
    active: PlaybackSession | None
    days: dict

    def day(self, account=None):
        return self.days.get(account, self.days[None])

    @property
    def expires_at(self):
        return min(*(end for _, end in self.days.values()),
                   self.active.expires_at if self.active else float('inf'))

    def read(self, plays):
        current, today = {}, {}
        for row in plays:
            key, at, account = row['identity_key'], row['played_at'], row.get('account_id')
            if at > self.now:
                continue
            start, _end = self.day(account)
            if at >= start:
                today[key] = max(at, today.get(key, at))
            if (self.active and account == self.active.account and at >= self.active.started_at
                    and row.get('live', True)):
                current[key] = max(at, current.get(key, at))
        return LayerPlays(current, today)


@dataclass(frozen=True)
class LayerPlays:
    current: dict
    today: dict

    def earlier_today(self):
        return {key: at for key, at in self.today.items() if key not in self.current}


@dataclass
class RecentEvidence:
    latest: dict
    modes: list
    vectors: object
    index: dict
    layers: LayerPlays | None = None

    def since(self, timestamp):
        return {key: ts for key, ts in self.latest.items() if ts >= timestamp}


def _classified_mode_plays(evidence, latest, min_events):
    """Nearest active mode and latest timestamp per usable key, or None below the gate."""
    modes, CV, cidx = evidence.modes, evidence.vectors, evidence.index
    if not modes or CV is None:
        return None
    played_keys = [key for key in latest if key in cidx]
    if len(played_keys) < min_events:
        return None

    mode_ids = [mode["mode_id"] for mode in modes]
    C = np.stack([mode["centroid"].astype(np.float64) for mode in modes])
    if C.shape[1] != CV.shape[1]:
        # Content preparation can change dimensions before the modes are rebuilt.
        # Read that stale-centroid window as quiet rather than crashing Home.
        return None
    return [(mode_ids[int((C @ CV[cidx[key]].astype(np.float64)).argmax())], latest[key])
            for key in played_keys]


def now_mode_mix(evidence, now, *, window_h, min_events):
    """Shared internals for `now_mode_posterior` and the taste_viz NOW ribbon: the {mode_id: share}
    posterior, `n` (the count of distinct played keys that fed it), and the active mode list used to
    build it (so a caller wanting mode labels doesn't have to re-fetch them).
    Returns (None, 0, modes) below the confidence gate - `modes` may still be non-empty even when the
    gate isn't cleared. Public (not `_`-prefixed): taste_viz needs the full mix, not just the reduced
    readings the other two callers take from it.
    """
    modes = evidence.modes
    latest = evidence.layers.current if evidence.layers else evidence.since(now - window_h * 3600.0)
    classified = _classified_mode_plays(evidence, latest, min_events)
    if classified is None:
        return None, 0, modes

    counts: dict = {}
    for mid, _timestamp in classified:
        counts[mid] = counts.get(mid, 0) + 1

    total = sum(counts.values())
    return {mid: c / total for mid, c in counts.items()}, total, modes


def now_mode_posterior(evidence, now, *, window_h, min_events) -> dict | None:
    """{mode_id: share} over the user's active taste modes, read from the last `now_window_h` hours of
    captured real plays, or None below the confidence gate.

    Plays are deduped per identity_key (each key's latest timestamp in the window wins, though the
    timestamp itself doesn't otherwise matter here: this layer classifies WHAT was played, not WHEN).
    Each distinct played key with a content vector is assigned to its nearest active-mode centroid by
    cosine (content vectors and mode centroids are both L2-unit, so cosine == dot; same nearest-
    centroid rule as mode_surfaces._nearest_mode). The posterior is each mode's share of that count,
    so shares sum to 1.0 over modes that received at least one play.

    Returns None when: there are no active taste modes; no content vector model has been built; or
    fewer than `now_min_events` distinct played keys in the window have a content vector. Plays are
    read from play_events ONLY (real timestamps), never coarse listening observations: a
    noon-bucket history row happening to fall inside the window carries no real-time information and
    must not count toward "right now".
    """
    posterior, _n, _modes = now_mode_mix(evidence, now, window_h=window_h, min_events=min_events)
    return posterior


def evidence_coverage(evidence, now, *, window_h) -> dict:
    """Coverage/confidence facts for the UI, computed from the exact NOW/Session play source.

    `total` is distinct eligible real-time plays after radio provenance exclusion; `mapped` is the
    subset with a content vector and therefore able to vote in the mode posterior.
    """
    cidx = evidence.index

    def one(latest):
        mapped = sum(k in cidx for k in latest)
        total = len(latest)
        return {"total": total, "mapped": mapped,
                "coverage": mapped / total if total else None}

    captured = evidence.layers or LayerPlays(evidence.since(now - window_h * 3600),
                                              evidence.since(now - 24 * 3600))
    return {"now": one(captured.current), "session": one(captured.today)}


def session_tilt(latest, now, V, idx, *, min_events, halflife_h, since=None) -> np.ndarray | None:
    """Bounded embedding tilt from plays in the current listening session: plays from the last
    24 hours (the kernel does the shaping), each wall-clock decayed by `session_halflife_h` (hours).
    Same transient-model sibling as centroid_tilt: accumulates wall-clock-decayed unit vectors,
    returns their mean with decay preserved, or None when quiet/below gate.

    Plays are deduped per identity_key (each key's latest timestamp in the window wins). Only plays
    whose keys exist in idx (the CALLER'S vector index: scoring passes the collaborative V/idx, the
    same space centroid_tilt tilts) contribute. Returns None when: no recent play is in that index;
    fewer than `now_min_events` contributing keys (the shared confidence gate); or zero-norm
    accumulation. Decay is per-event and internal; callers apply no external freshness factor,
    mirroring centroid_tilt.
    """
    lower = now - 24 * 3600 if since is None else since
    played_keys = [k for k, at in latest.items() if k in idx and lower <= at <= now]
    if len(played_keys) < min_events:
        return None

    # Divide by the number of usable directions, independent of age, so quiet hours reduce
    # magnitude rather than being canceled by normalization (#85/#45).
    tilt = np.zeros(V.shape[1], dtype=np.float64)
    contributing = 0
    for k in played_keys:
        ts = latest[k]
        v = V[idx[k]]
        nrm = np.linalg.norm(v)
        if nrm == 0:
            continue
        # session_halflife_h is in hours; decay_weight expects half_life_d in days.
        weight = decay_weight(now - ts, halflife_h / 24.0) if halflife_h is not None else 1.
        tilt += weight * (v / nrm)
        contributing += 1

    if contributing < min_events or not np.any(tilt):
        return None
    return tilt / contributing


def session_mode_mix(evidence, now, *, min_events, halflife_h):
    """The SESSION layer's legible companion to `session_tilt`. `session_tilt` blends recent plays
    into a single bounded tilt in the collaborative embedding space - real signal, but not something
    that can be honestly labeled (a blend of two unrelated corners of taste is a "free vector" that
    points at neither, the same reason `now_mode_posterior` classifies rather than averages). This
    function anchors the same session-scale evidence to the user's real, previously-discovered taste
    modes instead, exactly like `now_mode_mix` does for the NOW layer: same content-vector space, same
    nearest-mode-centroid classification, same dedup-latest-per-key, same `now_min_events` confidence
    gate. The only difference is the window and the weighting - a 24-hour window (vs NOW's
    `now_window_h`), with each played key's classification contributing
    `transient.decay_weight(age, session_halflife_h / 24.0)` (fresher plays count for more) instead of
    a flat 1, so the mix reads as "this session's mode balance", decaying smoothly rather than falling
    off a window edge.

    Returns (shares, n, modes): `shares` is {mode_id: share} normalized to sum to 1.0 (weighted by
    decay, not raw count), or None below the gate; `n` is the number of distinct played keys that
    contributed (unweighted, matching `now_mode_mix`'s `n`); `modes` is the active mode list. Below the
    gate this returns None, never a weak guess, mirroring every other layer in this module.
    """
    modes = evidence.modes
    latest = evidence.layers.today if evidence.layers else evidence.since(now - 24 * 3600.0)
    classified = _classified_mode_plays(evidence, latest, min_events)
    if classified is None:
        return None, 0, modes

    weights: dict = {}
    for mid, timestamp in classified:
        weights[mid] = weights.get(mid, 0.0) + decay_weight(now - timestamp, halflife_h / 24.0)

    total = sum(weights.values())
    n = len(classified)
    if total <= 0:
        return None, n, modes
    return {mid: w / total for mid, w in weights.items()}, n, modes
