"""The enrichment waterfall: run the configured providers, in order, over a set of tracks.

This is the reusable orchestrator the background worker will call. For each track it walks the
enabled providers in the user's configured order; each provider's read-only ``probe`` returns what it
found, which the harness (a) writes to the parseable ``enrichment_log``, (b) applies to the canonical
track fill-only (so a later provider sees an MBID an earlier one resolved) and (c) compares across
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


class TrackSink:
    """Default persistence sink: write a provider's findings onto the canonical LIBRARY track (by id),
    log every field, and record cross-provider conflicts. This is the pre-#50 behavior, unchanged."""
    def __init__(self, store, track):
        self.store, self.track = store, track

    def set_enrichment(self, genre, year):
        self.store.set_track_enrichment(self.track["id"], genre, year)

    def set_mbid(self, mbid):
        self.store.set_track_mbid(self.track["id"], mbid)
        self.track["mb_recording_id"] = self.track.get("mb_recording_id") or mbid

    def set_audio(self, **audio):
        self.store.set_track_audio(self.track["id"], **audio)

    def log(self, run_id, provider, field, value):
        self.store.log_enrichment(self.track["id"], run_id, provider, field, value)

    def upsert_conflict(self, field, candidates):
        self.store.upsert_conflict(self.track["id"], field, candidates)

    def effective_enrichment(self):
        return self.store.get_track_enrichment(self.track["id"])


class DiscoveredSink:
    """#50 cold sink: write genre/year/audio onto a discovered_tracks row (by identity_key). Discovered
    tracks are candidates, not canonical library rows, so MBID, the enrichment log, and conflict review
    are no-ops. Remembers the last genre/year it set so effective_enrichment can echo it for progress."""
    def __init__(self, store, identity_key):
        self.store, self.key = store, identity_key
        self._genre = self._year = None

    def set_enrichment(self, genre, year):
        if genre is not None:
            self._genre = genre
        if year is not None:
            self._year = year
        self.store.set_discovered_enrichment(self.key, genre, year)

    def set_mbid(self, mbid):
        pass

    def set_audio(self, **audio):
        self.store.set_discovered_audio(self.key, **audio)

    def log(self, run_id, provider, field, value):
        pass

    def upsert_conflict(self, field, candidates):
        pass

    def effective_enrichment(self):
        return (self._genre, self._year)


def _apply(sink, track, res) -> None:
    """Fill-only-apply a provider's findings through `sink`, and mirror an mb_recording_id onto the
    in-memory track dict so later providers in this run can key off it (the sink handles persistence)."""
    f = res.fields
    if "genre" in f or "year" in f:
        sink.set_enrichment(f.get("genre"), f.get("year"))
    if f.get("mb_recording_id"):
        sink.set_mbid(f["mb_recording_id"])
        track["mb_recording_id"] = track.get("mb_recording_id") or f["mb_recording_id"]
    audio = {k: v for k, v in f.items() if k in _AUDIO}
    if audio:
        sink.set_audio(**audio)


def run_waterfall(store, tracks, config, on_progress, should_stop=None, run_id=None, registry=None,
                  sink_for=None):
    """Enrich `tracks` through the enabled providers in `config` order. `config` is the list from
    enrichment.load_config(store). `registry` (name -> provider module) is injectable for tests.
    `sink_for(track) -> sink` selects per-track persistence: the default builds a TrackSink (library
    tracks); the cold path (#50) passes one that builds a DiscoveredSink (discovered_tracks)."""
    registry = registry or REGISTRY
    sink_for = sink_for or (lambda t: TrackSink(store, t))
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
                         "text": f"{p.get('label', p['name'])} is enabled but has no API key, skipping."})
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
    conflicts_found = 0
    try:
        for i, t in enumerate(tracks, 1):
            if should_stop and should_stop():
                on_progress({"type": "info", "text": "Stopped."})
                return
            _gate.wait_turn(seq, on_wait=lambda: on_progress(
                {"type": "info", "text": "Waiting: a newer run is enriching first…"}))
            sink = sink_for(t)
            results = []
            for m in chosen:
                if m.name in dead:
                    continue
                res = m.probe(t, store)
                for fld, val in res.fields.items():
                    sink.log(run_id, m.name, fld, val)
                _apply(sink, t, res)
                results.append(res)
                if m.tripped():                   # host unreachable. Drop it for the rest of the run
                    dead.add(m.name)
                    on_progress({"type": "info", "text": f"{m.name} looks unreachable. "
                                 "Skipping it for the rest of this run."})
            for fld, candidates in base.detect_conflicts(results).items():
                sink.upsert_conflict(fld, candidates)
                conflicts_found += 1
            eff_genre, eff_year = sink.effective_enrichment()
            on_progress({"type": "track", "i": i, "n": total, "video_id": t["video_id"],
                         "genre": eff_genre, "year": eff_year, "text": f"{i}/{total} {t['title']}"})
        note = f" · {conflicts_found} disagreement(s) to review" if conflicts_found else ""
        on_progress({"type": "done", "text": f"Enriched {total} track(s).{note}",
                     "total": total, "conflicts": conflicts_found})
    finally:
        _gate.leave(seq)
