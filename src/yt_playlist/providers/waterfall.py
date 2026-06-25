"""The enrichment waterfall: run the configured providers, in order, over a set of tracks.

This is the reusable orchestrator the background worker will call. For each track it walks the
enabled providers in the user's configured order; each provider's read-only ``probe`` returns what it
found, which the harness (a) writes to the parseable ``enrichment_log``, (b) applies to the canonical
track fill-only — so a later provider sees an MBID an earlier one resolved — and (c) compares across
providers to record disagreements as ``enrichment_conflict`` rows.

Web-agnostic: it takes a track list, an ``on_progress`` callback (same event shapes the SSE renderer
already understands), and an optional ``should_stop``. A single PriorityGate lets the newest run
preempt older ones at track boundaries.
"""
import uuid

from yt_playlist.providers import musicbrainz, lastfm, discogs, deezer, acousticbrainz
from yt_playlist.providers import base
from yt_playlist.providers.enrich_queue import PriorityGate

REGISTRY = {m.name: m for m in (musicbrainz, lastfm, discogs, deezer, acousticbrainz)}
_gate = PriorityGate()

# Result fields routed to set_track_audio (everything except genre/year/mb_recording_id).
_AUDIO = ("bpm", "energy", "danceability", "music_key", "music_scale", "mood_happy", "mood_sad",
          "mood_relaxed", "mood_acoustic", "instrumental", "loudness", "dynamic_complexity",
          "popularity", "gain", "label")


def _apply(store, track, res) -> None:
    """Fill-only-apply a provider's findings to the canonical track, and mirror an mb_recording_id
    onto the in-memory track dict so later providers in this run can key off it."""
    f = res.fields
    if "genre" in f or "year" in f:
        store.set_track_enrichment(track["id"], f.get("genre"), f.get("year"))
    if f.get("mb_recording_id"):
        store.set_track_mbid(track["id"], f["mb_recording_id"])
        track["mb_recording_id"] = track.get("mb_recording_id") or f["mb_recording_id"]
    audio = {k: v for k, v in f.items() if k in _AUDIO}
    if audio:
        store.set_track_audio(track["id"], **audio)


def run_waterfall(store, tracks, config, on_progress, should_stop=None, run_id=None, registry=None):
    """Enrich `tracks` through the enabled providers in `config` order. `config` is the list from
    enrichment.load_config(store). `registry` (name -> provider module) is injectable for tests."""
    registry = registry or REGISTRY
    run_id = run_id or uuid.uuid4().hex
    chosen = []
    for p in config:
        if not p.get("enabled"):
            continue
        mod = registry.get(p["name"])
        if mod is None:
            continue
        if not mod.available(store):
            on_progress({"type": "info",
                         "text": f"{p.get('label', p['name'])} is enabled but has no API key — skipping."})
            continue
        chosen.append(mod)

    total = len(tracks)
    if not total:
        on_progress({"type": "done", "text": "Everything is already enriched.", "total": 0})
        return
    if not chosen:
        on_progress({"type": "done", "text": "No enrichment providers are enabled.", "total": 0})
        return

    for m in chosen:
        m.reset()
    names = ", ".join(m.name for m in chosen)
    on_progress({"type": "info", "text": f"Enriching {total} track(s) via {names}…", "total": total})
    seq = _gate.enter()
    dead = set()                                  # providers whose breaker tripped mid-run
    try:
        for i, t in enumerate(tracks, 1):
            if should_stop and should_stop():
                on_progress({"type": "info", "text": "Stopped."})
                return
            _gate.wait_turn(seq, on_wait=lambda: on_progress(
                {"type": "info", "text": "Waiting — a newer run is enriching first…"}))
            results = []
            for m in chosen:
                if m.name in dead:
                    continue
                res = m.probe(t, store)
                for fld, val in res.fields.items():
                    store.log_enrichment(t["id"], run_id, m.name, fld, val)
                _apply(store, t, res)
                results.append(res)
                if m.tripped():                   # host unreachable — drop it for the rest of the run
                    dead.add(m.name)
                    on_progress({"type": "info", "text": f"{m.name} looks unreachable — "
                                 "skipping it for the rest of this run."})
            for fld, candidates in base.detect_conflicts(results).items():
                store.upsert_conflict(t["id"], fld, candidates)
            eff_genre, eff_year = store.get_track_enrichment(t["id"])
            on_progress({"type": "track", "i": i, "n": total, "video_id": t["video_id"],
                         "genre": eff_genre, "year": eff_year, "text": f"{i}/{total} {t['title']}"})
        on_progress({"type": "done", "text": f"Enriched {total} track(s).", "total": total})
    finally:
        _gate.leave(seq)
