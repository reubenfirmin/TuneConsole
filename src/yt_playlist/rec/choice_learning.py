"""Bounded context influence from deliberate choices, separate from organic track learning."""
import time

from yt_playlist.rec import rec_params


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
        'choice_recent_w', 'choice_halflife_d', 'choice_saved_w', 'choice_saved_days')}
    for latest in projection.get("contexts", []):
        if latest['format'] == 'mixes' and latest.get('descriptor_id') is None:
            continue  # Old selected-track projections are not broad descriptor evidence.
        if latest["at"] > now:
            continue
        age = max(0, now - latest["at"])
        # Correlated Radio branches or rerenders cannot supply one unit per decision/track.
        support = latest["episode_support"]
        current = parameters['now_gain'] if age < parameters['now_window_h'] * 3600 else 0.
        session = (parameters['session_alpha'] * 2 ** (-age / (3600 * parameters['session_halflife_h']))
                   if age < 86400 else 0.)
        recent = parameters['choice_recent_w'] * 2 ** (-age / (86400 * parameters['choice_halflife_d']))
        saved = parameters['choice_saved_w'] if latest['days'] >= parameters['choice_saved_days'] else 0.
        weights = {"now": support * current, "session": support * session,
                   "recent": support * recent, "saved": support * saved}
        if sum(weights.values()) < .001:
            continue
        yield {**latest, "layer_weights": weights, "weight": sum(weights.values())}
