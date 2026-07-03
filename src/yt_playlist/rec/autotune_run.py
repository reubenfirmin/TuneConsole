"""Run Auto-tune and record a UI-facing result (what it found on the user's corpus)."""
import json

from yt_playlist.rec import eval_recs, recommend

RESULT_SETTING = "rec_autotune_result"
_SNAPSHOT_N = 15


def _picks(store, now):
    return [{"title": it.title, "artist": it.artist}
            for it in recommend.for_you(store, now, limit=_SNAPSHOT_N)]


def run_and_record(store, now) -> dict:
    """Snapshot top picks, run Auto-tune (rebuilds vectors), snapshot again, diff, persist, return."""
    before = _picks(store, now)
    tuned = eval_recs.autotune(store)
    after = _picks(store, now)
    bset = {(p["title"], p["artist"]) for p in before}
    aset = {(p["title"], p["artist"]) for p in after}
    result = {
        "ran_at": now,
        "winner": tuned["winner"],
        "previous": tuned["previous"],
        "grid": tuned["grid"],
        "metric": tuned["metric"],           # #83: the one metric the whole sweep was judged on
        "in_sample": tuned["in_sample"],
        "recs": {
            "dropped": [{"title": t, "artist": a} for (t, a) in bset - aset],
            "added": [{"title": t, "artist": a} for (t, a) in aset - bset],
            "compared": len(before),
        },
    }
    if tuned.get("sweep_failed"):
        result["sweep_failed"] = True
    store.set_setting(RESULT_SETTING, json.dumps(result))
    return result


def last_result(store) -> dict | None:
    raw = store.get_setting(RESULT_SETTING)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None
