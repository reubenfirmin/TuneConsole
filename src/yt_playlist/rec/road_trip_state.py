"""The persisted JSON draft contract and its local normalization/view operations.

Transitions in road_trip mutate and return this same mapping. The web application owns persistence,
generation checks and job lifetime. No job handles, clients or library handles belong in this JSON.
Optional fields are populated lazily; normalization retains unknown fields from older saved drafts.
"""
from typing import NotRequired, TypedDict

from yt_playlist.rec import road_trip_axes as axes
from yt_playlist.rec.road_trip_mix import AVG_TRACK_S, _apportion_percentages, _mix_source, _mix_weights

MAX_OTHER_POOL = 44
MIN_ARTIST_CAP = 4
ROW_FIELDS = ("video_id", "title", "artist", "album", "thumbnail", "duration", "source",
              "genre", "family", "year", "decade", "cluster_genre")


class DraftState(TypedDict):
    recipe_id: int
    name: str
    seed: int
    own_pct: int
    familiarity_pct: int
    blacklist_genres: list[str]
    target_minutes: int
    inputs: dict[str, list[str]]
    pool: list[dict]
    picked: list[dict]
    picks: list[str]
    banned: list[str]
    prev: list[str]
    targets: dict[str, dict[str, float]]
    axes: dict[str, list[dict]]
    stats: dict
    building: bool
    phase: str | None
    pending: list[dict]
    done_inputs: list[str]
    own_facts: dict[str, dict]
    own_facts_left: int
    other_limit: int
    other_cap: int
    saved_playlist_id: str | None
    artist_contexts: NotRequired[dict[str, dict]]
    own_facts_done: NotRequired[int]
    done_widening: NotRequired[list[str]]
    untagged: NotRequired[dict[str, int]]
    blend_available: NotRequired[bool]
    mix_version: NotRequired[int]
    build_error: NotRequired[str | None]


def normalize_shape(state: DraftState) -> DraftState:
    """Supply legacy defaults in place, without acquisition or redrawing a saved playlist."""
    # The live library now supplies your side on every repick; discard its stale cached copy.
    state["pool"] = [c for c in state.get("pool") or [] if c.get("source") != "mine"]
    for candidate in state["pool"]:
        candidate.setdefault("cluster_genre", "")
    state.setdefault("own_facts", {})
    for key, default in (("phase", None), ("pending", []), ("done_inputs", []), ("banned", []),
                         ("prev", []), ("building", False), ("build_error", None),
                         ("saved_playlist_id", None), ("own_facts_left", 0), ("own_facts_done", 0),
                         ("familiarity_pct", 50), ("other_limit", MAX_OTHER_POOL),
                         ("other_cap", MIN_ARTIST_CAP)):
        state.setdefault(key, default)
    state.setdefault("targets", {})
    state.setdefault("untagged", {})
    for party in ("mine", "theirs"):
        state["targets"].setdefault(party, {})
        for axis in state.setdefault("axes", {}).setdefault(party, []):
            axis.setdefault("target", state["targets"][party].get(axis.get("key")))
            axis.setdefault("share", 0.0)
        state["axes"][party] = axes.order(state["axes"][party])
    return state


def draft_tracks(state: DraftState) -> list[dict]:
    """Full ordered rows ready for rendering or playlist_generated.create_generated_playlist."""
    return list(state.get("picked") or [])


def restat(state: DraftState) -> None:
    """Refresh counts and duration after a transition, preserving its insufficiency report."""
    picked = draft_tracks(state)
    for candidate in picked:
        candidate["mix_source"] = _mix_source(candidate, state)
    mine = [c for c in picked if c["source"] == "mine"]
    theirs = [c for c in picked if c["source"] != "mine"]
    yours = [c for c in picked if c["mix_source"] == "yours"]
    blend = [c for c in picked if c["mix_source"] == "blend"]
    state.setdefault("blend_available", bool(blend))

    def mins(rows):
        return round(sum((c["duration"] or AVG_TRACK_S) for c in rows) / 60)

    state["stats"] = {**state.get("stats", {}), "minutes": mins(picked),
                      "own_count": len(mine), "their_count": len(theirs),
                      "own_minutes": mins(mine), "their_minutes": mins(theirs),
                      "yours_count": len(yours), "blend_count": len(blend),
                      "yours_minutes": mins(yours), "blend_minutes": mins(blend),
                      "overlap_count": len(blend),
                      "mix_targets": _apportion_percentages(_mix_weights(state), len(picked))}
