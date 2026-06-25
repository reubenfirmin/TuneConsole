"""The provider interface the enrichment harness talks to, plus the shared result type it compares.

Each metadata provider exposes a read-only ``probe(track, store) -> EnrichmentResult`` that returns
only what it *found* (never writing to the store). The harness logs every finding, fills the
canonical track fill-only, and compares results across providers to surface disagreements. Field
comparison is declared in ``FIELD_SPECS`` so the harness stays provider-agnostic.

A "field" is a neutral concept name (``genre``, ``year``, ``bpm``, …), not a DB column — the store
layer maps it to a column (``year`` -> ``mb_year``). Fields absent from ``FIELD_SPECS`` are logged
and filled but never conflict-checked (e.g. single-source audio features can't disagree).
"""
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
