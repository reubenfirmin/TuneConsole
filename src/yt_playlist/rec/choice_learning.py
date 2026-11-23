"""Bounded context influence from deliberate choices, separate from organic track learning."""
import time

from yt_playlist.library.listening_window import choice_timezone, listening_window
from yt_playlist.rec import rec_params
from yt_playlist.rec.layers import calendar_window


def interest_key(row):
    if row['format'] == 'mixes':
        return ('mixes', row.get('descriptor_id'))
    return (row["format"], row.get("basis"), row.get("mode_id"), row.get("intent"),
            row["option_id"] if row["format"] == "albums" or row.get("mode_id") is None else None)


def contexts(store, now=None):
    now = time.time() if now is None else now
    return store.memoized_read(('choice-contexts', now), lambda: _contexts(store, now))


def _contexts(store, now):
    projection = store.rec_proposals.get_proposals("choice_interests") or {}
    return list(_weighted_contexts(store, projection, now))


def transient_active(store, projection, now):
    """Use the same evidence filters and decay as serving, with scalar-only context rows."""
    active = False
    for row in _weighted_contexts(store, projection, now):
        if sum(row['layer_weights'][layer] for layer in ('now', 'session', 'recent')) >= .001:
            active = True
    return active


def _weighted_contexts(store, projection, now):
    if not projection.get('contexts'):
        return
    parameters = {name: rec_params.get_param(store, name) for name in (
        'now_gain', 'now_window_h', 'session_alpha', 'session_halflife_h',
        'choice_recent_w', 'choice_halflife_d', 'choice_saved_w', 'choice_saved_days', 'recent_horizon_days')}
    if projection.get('schema', 1) >= 2:
        window = listening_window(store, now)
        day = calendar_window(now, choice_timezone(store))
        yield from weighted_contexts(projection, now, parameters, window=window, day=day)
    else:
        yield from weighted_contexts(projection, now, parameters)


def weighted_contexts(projection, now, parameters, *, window=None, day=None):
    """Read verified contexts using the clock policy recorded with the projection."""
    for latest in projection.get("contexts", []):
        if latest['format'] == 'mixes' and latest.get('descriptor_id') is None:
            continue  # Old selected-track projections are not broad descriptor evidence.
        if latest["at"] > now:
            continue
        age = max(0, now - latest["at"])
        # Correlated Radio branches or rerenders cannot supply one unit per decision/track.
        support = latest["episode_support"]
        if window is None:  # Version-one projections remain readable until their replacement is published.
            current = parameters['now_gain'] if age < parameters['now_window_h'] * 3600 else 0.
            session = parameters['session_alpha'] if age < 86400 else 0.
            recent = parameters['choice_recent_w']
            sustained = latest['days'] >= parameters['choice_saved_days']
        else:
            account = latest.get('choice_account_id')
            start = latest.get('choice_start_at')
            session_at = max(latest['at'], start) if start is not None and start <= now else latest['at']
            in_session = (window.active is not None and (account is None or account == window.active.account)
                          and session_at >= window.active.started_at)
            current = parameters['now_gain'] if in_session else 0.
            start_of_day = window.day(account)[0] if account is not None else day[0]
            session = parameters['session_alpha'] if not current and session_at >= start_of_day else 0.
            recent = (parameters['choice_recent_w'] if not current and not session
                      and age <= 86400 * parameters['recent_horizon_days'] else 0.)
            minimum = max(3, parameters['choice_saved_days'])
            sustained = (min(latest['days'], latest['episodes']) >= minimum
                         and latest.get('sustained', True))
        session *= 2 ** (-age / (3600 * parameters['session_halflife_h']))
        recent *= 2 ** (-age / (86400 * parameters['choice_halflife_d']))
        saved = parameters['choice_saved_w'] if sustained else 0.
        weights = {"now": support * current, "session": support * session,
                   "recent": support * recent, "saved": support * saved}
        if sum(weights.values()) < .001:
            continue
        yield {**latest, "layer_weights": weights, "weight": sum(weights.values())}
