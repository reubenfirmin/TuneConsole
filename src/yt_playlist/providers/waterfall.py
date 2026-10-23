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
import time
from collections.abc import Mapping

from yt_playlist.providers import musicbrainz, lastfm, discogs, deezer, acousticbrainz
from yt_playlist.providers import base, coverart
from yt_playlist.providers.enrich_queue import PriorityGate
from yt_playlist.util.thumbnails import is_placeholder_art
from yt_playlist.util import net
from yt_playlist.util.matching import identity_key

REGISTRY: dict[str, base.Provider] = {
    m.name: m for m in (musicbrainz, lastfm, discogs, deezer, acousticbrainz)}
_gate = PriorityGate()

# Plumbing a provider hands to another step rather than a finding about the track. Never logged (it
# would pollute the field comparison UI). Retained in the full provider attempt evidence.
_INTERNAL = ("mb_release_ids",)

# Providers that return a ready-to-use cover URL, in priority order. Art is deliberately NOT applied
# per-provider like the other fields: providers run in the user's configured order (musicbrainz
# first, deezer last, by default), so a fill-only write would let whoever ran first win. Art is
# picked once per track, after every provider has answered, by THIS order instead.
_ART_SOURCES = ("deezer",)


class TrackSink:
    """Persist shared findings and item-scoped evidence for either catalog membership."""
    def __init__(self, store, track, providers=()):
        self.store, self.track = store, track
        self.providers = providers

    def apply_fields(self, provider, fields):
        self.store.enrichment.apply_provider_fields(self.track['id'],provider,fields,
                                                   providers=self.providers)

    def set_art(self, url, *, source):
        self.store.tracks.set_track_art(self.track["id"], url, source=source)

    def record_artwork(self, release_ids, outcome, now):
        self.store.enrichment_state.record_artwork_result(self.track["id"], release_ids, outcome, now)

    def log(self, run_id, provider, field, value):
        self.store.enrichment_state.log_enrichment(self.track["id"], run_id, provider, field, value)

    def upsert_conflict(self, field, candidates, *, refreshed_providers=None):
        self.store.enrichment.upsert_conflict(self.track["id"], field, candidates,
                                   refreshed_providers=refreshed_providers)

    def retire_agreed_conflicts(self, evidence):
        self.store.enrichment.retire_agreed_conflicts(self.track["id"], evidence)

    def effective_enrichment(self):
        return self.store.tracks.get_track_enrichment(self.track["id"])


def _apply(sink, track, res) -> None:
    """Fill-only-apply a provider's findings through `sink`, and mirror an mb_recording_id onto the
    in-memory track dict so later providers in this run can key off it (the sink handles persistence)."""
    sink.apply_fields(res.provider, res.fields)
    if res.fields.get('mb_recording_id'):
        track['mb_recording_id'] = sink.store.tracks.track_for_waterfall(track['id'])['mb_recording_id']


def needs_art(track) -> bool:
    """No cover worth keeping? A track dict without a `thumbnail` key predates this column in the
    pending query, so treat it as needing art and let the fill-only sink decide."""
    thumb = track.get("thumbnail")
    return not thumb or is_placeholder_art(thumb)


def _pick_art(results):
    """A cover from the highest-priority provider that found one."""
    for name in _ART_SOURCES:
        for r in results:
            art = r.fields.get("art")
            if r.provider == name and art:
                return art
    return None


def _resolve_art(sink, track, results, now_fn=time.time):
    """Give the track a cover if it has none. YouTube serves a generic grey disc rather than nothing
    when it has no art (always, for your own uploads), so a thumbnail is not proof of a cover.

    Cover Art Archive is the fallback only: it costs an HTTP call per candidate release, so it runs
    only when the primary found nothing AND the track actually needs art."""
    if not needs_art(track):
        sink.record_artwork([], coverart.CoverResult("found", track.get("thumbnail")), now_fn())
        return
    art = _pick_art(results)
    release_ids = next((r.fields["mb_release_ids"] for r in results
                        if r.fields.get("mb_release_ids")), [])
    outcome = coverart.CoverResult("found", art) if art else coverart.CoverResult("absent")
    if not art:
        if release_ids:
            outcome = coverart.lookup(release_ids)
    sink.set_art(outcome.url, source='deezer' if art else 'coverart')
    sink.record_artwork(release_ids, outcome, now_fn())
    return outcome


def run_waterfall(store, tracks, config, on_progress, should_stop=None, run_id=None,
                  registry: Mapping[str, base.Provider] | None = None,
                  sink_for=None, now_fn=time.time, reuse_completed=False):
    """Enrich `tracks` through the enabled providers in `config` order. `config` is the list from
    enrichment.load_config(store). `registry` (name -> provider module) is injectable for tests.
    `sink_for(track) -> sink` supports injected persistence; the default TrackSink writes to the
    shared catalog regardless of library/discovery membership.

    Returns explicit recording job IDs (entry IDs for direct callers without a job) for which every selected/available
    provider answered without request failures. A circuit breaker can skip later tracks in the batch;
    callers must not mark those tracks processed, because they still need a prompt retry."""
    registry = registry or REGISTRY
    sink_for = sink_for or (lambda t: TrackSink(store, t, [m.name for m in chosen]))
    run_id = run_id or uuid.uuid4().hex
    labels = {p["name"]: p.get("label", p["name"]) for p in config}
    chosen: list[base.Provider] = []
    for p in config:
        if not p.get("enabled"):
            continue
        mod = registry.get(p["name"])
        if mod is None:
            continue
        if not mod.available(store):
            on_progress({"type": "info",
                         "warning": True,
                         "text": f"{p.get('label', p['name'])} is enabled but has no API key, skipping."})
            continue
        chosen.append(mod)

    total = len(tracks)
    if not total:
        on_progress({"type": "done", "text": "Everything is already enriched.", "total": 0})
        return []
    if not chosen:
        on_progress({"type": "done", "text": "No enrichment providers are available. Check Setup.",
                     "blocked": True, "total": 0})
        return []

    cooldowns = store.enrichment_state.enrichment_cooldowns()
    for m in chosen:
        if cooldowns.get(m.name, {}).get("retry_at", 0) <= now_fn():
            m.reset()
    names = ", ".join(m.name for m in chosen)
    on_progress({"type": "info", "text": f"Enriching {total} track(s) via {names}…", "total": total})
    seq = _gate.enter()
    dead = set()                                  # providers whose breaker tripped mid-run
    conflicts_found = 0
    fully_attempted = []
    try:
        for i, t in enumerate(tracks, 1):
            if should_stop and should_stop():
                on_progress({"type": "info", "text": "Stopped."})
                return fully_attempted
            _gate.wait_turn(seq, on_wait=lambda: on_progress(
                {"type": "info", "waiting": True,
                 "text": "Waiting: a newer run is enriching first…"}))
            t = {**t, **(store.tracks.catalog_subject(t["id"]) or {})}
            sink = sink_for(t)
            if isinstance(sink, TrackSink):
                sink.providers = [m.name for m in chosen]
            results = []
            successful = set()
            attempted = 0
            key = t.get("identity_key") or identity_key(t["title"], t.get("artist", ""))
            coverage = t.get("provider_jobs")
            saved = store.enrichment_state.enrichment_provider_state(t["recording_id"], include_previous=coverage is not None)
            if coverage is not None:
                # Historical attempts predate explicit coverage scheduling. Their retained answers
                # still supply context while only the newly available provider performs requests.
                saved = {**{name: attempt for name, attempt in store.enrichment_state.enrichment_attempts(key).items()
                            if name not in coverage}, **saved}
            for m in chosen:
                prior = saved.get(m.name, {})
                if coverage is not None and m.name not in coverage:
                    if prior.get("status") in ("found", "no_match"):
                        results.append(base.EnrichmentResult(m.name, prior["fields"]))
                        successful.add(m.name)
                    attempted += 1
                    continue
                if reuse_completed and prior.get("status") in ("found", "no_match"):
                    res = base.EnrichmentResult(m.name, prior["fields"])
                    _apply(sink, t, res)
                    results.append(res)
                    successful.add(m.name)
                    attempted += 1
                    continue
                if m.name in dead:
                    continue
                deadline = max(prior.get("retry_at", 0),
                               cooldowns.get(m.name, {}).get("retry_at", 0))
                if deadline > now_fn():
                    on_progress({"type": "info", "warning": True, "retry_at": deadline,
                                 "text": f"{labels[m.name]} deferred for {t['title']}; "
                                         f"retry in {max(1, int(deadline - now_fn()))} seconds."})
                    continue
                on_progress({"type": "activity", "provider": m.name, "title": t["title"],
                             "artist": t.get("artist", ""), "i": i, "n": total,
                             "text": f"Looking up {t['title']} with {labels[m.name]}."})
                fields = {}
                with net.probe_errors() as errors:
                    try:
                        res = m.probe(t, store)
                        fields = dict(res.fields)
                        for fld, val in fields.items():
                            if fld not in _INTERNAL:
                                sink.log(run_id, m.name, fld, val)
                        _apply(sink, t, res)
                        results.append(res)
                    except Exception as exc:
                        # One malformed answer must not abort healthy providers or later tracks.
                        net.record_probe_failure(exc)
                retry = bool(errors) or m.tripped()
                outage = m.tripped() or any(net.provider_outage(e) for e in errors)
                now = now_fn()
                error = "; ".join(str(e) or type(e).__name__ for e in errors)
                if m.tripped() and not error:
                    error = "Provider looks unreachable"
                store.enrichment_state.record_enrichment_attempt(
                    key, m.name, fields, retry=retry, video_id=t.get("video_id"), now=now,
                    error=error, cooldown=outage,
                    retry_after=max((net.retry_after(e, now) for e in errors), default=0),
                    providers=[mod.name for mod in chosen])
                if not retry:
                    attempted += 1
                    successful.add(m.name)
                else:
                    cooldowns = store.enrichment_state.enrichment_cooldowns()
                    state = store.enrichment_state.enrichment_provider_state(t["recording_id"])[m.name]
                    on_progress({"type": "info", "warning": True,
                                 "retry_at": state["retry_at"],
                                 "text": f"{labels[m.name]} lookup failed for {t['title']}: {error}. "
                                         f"Retry in {max(1, int(state['retry_at'] - now))} seconds."})
                if m.tripped():                   # host unreachable. Drop it for the rest of the run
                    dead.add(m.name)
                    on_progress({"type": "info", "warning": True,
                                 "text": f"{m.name} looks unreachable. "
                                 "Skipping it for the rest of this run."})
            if needs_art(t):
                on_progress({"type": "activity", "title": t["title"],
                             "artist": t.get("artist", ""), "i": i, "n": total,
                             "text": f"Checking cover art for {t['title']}."})
            try:
                art_result = _resolve_art(sink, t, results, now_fn)
                if art_result and art_result.status == "retry":
                    on_progress({"type":"info", "warning":True,
                        "text": f"Artwork for {t['title']} will retry separately: {art_result.error}"})
            except Exception as exc:
                on_progress({"type": "info", "warning": True,
                             "text": f"Cover art lookup failed for {t['title']}: {exc}"})
            conflicts = base.detect_conflicts(results)
            for fld, candidates in conflicts.items():
                refreshed = {r.provider for r in results if r.provider in successful
                             and fld in r.fields and not base._empty(r.fields[fld])}
                sink.upsert_conflict(fld, candidates, refreshed_providers=refreshed)
                conflicts_found += 1
            if attempted == len(chosen):
                agreeing = {field: {r.provider: r.fields[field] for r in results
                                   if field in r.fields and not base._empty(r.fields[field])}
                            for field in base.FIELD_SPECS if field not in conflicts}
                sink.retire_agreed_conflicts(agreeing)
            eff_genre, eff_year = sink.effective_enrichment()
            if attempted == len(chosen):
                fully_attempted.append(t.get("job_id", t["id"]))
            on_progress({"type": "track", "i": i, "n": total, "video_id": t["video_id"],
                         "track_id": t.get("id"), "job_id": t.get("job_id", t["id"]), "identity_key": t.get("identity_key"),
                         "fully_attempted": attempted == len(chosen),
                         "genre": eff_genre, "year": eff_year, "text": f"{i}/{total} {t['title']}"})
        note = f" · {conflicts_found} disagreement(s) to review" if conflicts_found else ""
        remaining = total - len(fully_attempted)
        retry_note = f" {remaining} track(s) remain queued for retry." if remaining else ""
        on_progress({"type": "done", "text": f"Checked {len(fully_attempted)} track(s).{note}{retry_note}",
                     "total": len(fully_attempted), "conflicts": conflicts_found})
        return fully_attempted
    finally:
        _gate.leave(seq)
