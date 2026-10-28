"""Bounded context influence from deliberate choices, separate from organic track learning."""
import time

from yt_playlist.rec import rec_params


def interest_key(row):
    return (row["format"], row.get("basis"), row.get("mode_id"), row.get("intent"),
            row["option_id"] if row["format"] == "albums" or row.get("mode_id") is None else None)


def contexts(store, now=None):
    now = time.time() if now is None else now
    projection = store.rec_proposals.get_proposals("choice_interests") or {}
    result = []
    for latest in projection.get("contexts", []):
        if latest["at"] > now:
            continue
        age = max(0, now - latest["at"])
        # Correlated Radio branches or rerenders cannot supply one unit per decision/track.
        support = latest["episode_support"]
        current = rec_params.get_param(store, "now_gain") if age < rec_params.get_param(store, "now_window_h") * 3600 else 0.
        session = (rec_params.get_param(store, "session_alpha") * 2 ** (-age / (3600 * rec_params.get_param(store, "session_halflife_h")))
                   if age < 86400 else 0.)
        recent = rec_params.get_param(store, "choice_recent_w") * 2 ** (-age / (86400 * rec_params.get_param(store, "choice_halflife_d")))
        saved = (rec_params.get_param(store, "choice_saved_w") if latest["days"] >= rec_params.get_param(store, "choice_saved_days") else 0.)
        weights = {"now": support * current, "session": support * session,
                   "recent": support * recent, "saved": support * saved}
        if sum(weights.values()) < .001:
            continue
        result.append({**latest, "layer_weights": weights,
                       "weight": sum(weights.values())})
    return result
