"""The provider interface the enrichment harness talks to, plus the shared result type it compares.

Each metadata provider exposes a read-only ``probe(track, store) -> EnrichmentResult`` that returns
only what it *found* (never writing to the store). The harness logs every finding, fills the
canonical track fill-only, and compares results across providers to surface disagreements. Field
comparison is declared in ``FIELD_SPECS`` so the harness stays provider-agnostic.

A "field" is a neutral concept name (``genre``, ``year``, ``bpm``, …), not a DB column — the store
layer maps it to a column (``year`` -> ``mb_year``). Fields absent from ``FIELD_SPECS`` are logged
and filled but never conflict-checked (e.g. single-source audio features can't disagree).
"""
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class EnrichmentResult:
    provider: str                       # "musicbrainz"
    fields: dict = field(default_factory=dict)   # only non-empty findings: {"genre": "Rock", ...}


class Provider(Protocol):
    name: str

    def probe(self, track: dict, store) -> EnrichmentResult:
        """Look the track up and return findings, WITHOUT writing to the store."""
        ...

    def available(self, store) -> bool:
        """Is this provider usable right now (e.g. Last.fm has an API key)?"""
        ...

    def tripped(self) -> bool:
        """Has this provider's circuit breaker tripped (host looks unreachable)?"""
        ...

    def reset(self) -> None:
        """Reset the circuit breaker at the start of a run."""
        ...


# --- field comparison ---------------------------------------------------------------------------

class Discrete:
    """Two values agree iff equal (case-insensitively for strings)."""
    def agree(self, a, b) -> bool:
        if isinstance(a, str) and isinstance(b, str):
            return a.strip().casefold() == b.strip().casefold()
        return a == b

    def key(self, v):
        return v.strip().casefold() if isinstance(v, str) else v


class Numeric:
    """Two numbers agree iff within an absolute tolerance — trivial float drift isn't a conflict."""
    def __init__(self, tol):
        self.tol = tol

    def agree(self, a, b) -> bool:
        try:
            return abs(float(a) - float(b)) <= self.tol
        except (TypeError, ValueError):
            return a == b

    def key(self, v):
        try:
            return round(float(v) / (self.tol or 1.0))
        except (TypeError, ValueError):
            return v


# Only fields that >=2 providers can supply ever actually conflict; the rest are harmless to list.
FIELD_SPECS = {
    "genre": Discrete(),
    "year": Discrete(),
    "music_key": Discrete(),
    "music_scale": Discrete(),
    "label": Discrete(),
    "bpm": Numeric(2.0),
    "energy": Numeric(0.1),
    "danceability": Numeric(0.1),
}


def _empty(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def detect_conflicts(results) -> dict:
    """Given a list of EnrichmentResult for ONE track, return {field: candidates} for every
    conflict-checked field where >=2 providers returned disagreeing non-empty values.

    `candidates` is a list of {"provider", "value"} for all providers that had a value for the field
    (in result order), so the resolver UI can show every option, not just the disagreeing ones.
    """
    conflicts = {}
    for fname, spec in FIELD_SPECS.items():
        seen = [(r.provider, r.fields[fname]) for r in results
                if fname in r.fields and not _empty(r.fields[fname])]
        if len(seen) < 2:
            continue
        groups = {spec.key(v) for _, v in seen}
        if len(groups) >= 2:                       # genuine disagreement
            conflicts[fname] = [{"provider": p, "value": v} for p, v in seen]
    return conflicts


# --- shared provider plumbing -------------------------------------------------------------------

class RateLimiter:
    """Per-process pacer: serializes calls across threads and spaces them >= min_interval seconds
    apart, so we honour an external API's rate limit no matter how many enrichment threads run.
    Each provider owns one instance carrying its own interval (see each provider's _MIN_INTERVAL)."""

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self, min_interval=None):
        """Block until at least the interval has elapsed since the previous call. Pass min_interval
        to override the default for this call (e.g. Discogs paces differently with vs without a token)."""
        interval = self.min_interval if min_interval is None else min_interval
        with self._lock:                           # serialize + pace all of a provider's traffic
            wait = interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()


def run_enrich_loop(store, on_progress, pending, *, gate, breaker, start_text, empty_text,
                    done_text, wait_text, per_item, should_stop=None):
    """Shared skeleton for every provider's enrich_playlist(): short-circuit an empty set, announce
    the run, reset the circuit breaker, enter the priority gate, then walk `pending` (honouring
    should_stop and yielding to newer jobs) calling per_item(i, total, track) for the provider's
    own per-track work. A per_item returning False stops the run early (used when the breaker trips
    mid-run). start_text/done_text are called with the track count; empty_text/wait_text are strings."""
    total = len(pending)
    if not total:
        on_progress({"type": "done", "text": empty_text, "total": 0})
        return
    on_progress({"type": "info", "text": start_text(total), "total": total})
    breaker.reset()                            # fresh chance each run — a past outage shouldn't pre-trip it
    seq = gate.enter()
    try:
        for i, t in enumerate(pending, 1):
            if should_stop and should_stop():
                on_progress({"type": "info", "text": "Stopped."})
                return
            gate.wait_turn(seq, on_wait=lambda: on_progress({"type": "info", "text": wait_text}))
            if per_item(i, total, t) is False:     # provider signals a hard stop (host unreachable)
                return
        on_progress({"type": "done", "text": done_text(total), "total": total})
    finally:
        gate.leave(seq)
