"""Provider-neutral target matching for one-shot playlist imports."""
from dataclasses import dataclass

from yt_playlist.util.matching import fuzzy_ratio, normalize, track_artist
from yt_playlist.util.retry import with_retry
from yt_playlist.util.matching import identity_key
from yt_playlist.library.analysis import SYSTEM_PLAYLIST_IDS, jaccard


@dataclass(frozen=True)
class ImportResolution:
    source: object
    status: str                    # matched | ambiguous | unmatched
    target_video_id: str | None
    target_title: str | None
    target_artist: str | None
    score: float | None


@dataclass(frozen=True)
class ExistingOverlap:
    playlist_id: int
    title: str
    similarity: float
    exact: bool
    shared: int
    source_only: int
    existing_only: int


def resolve_track(target_client, source, fuzzy_threshold=.85, ambiguity_gap=.03) -> ImportResolution:
    """Resolve a neutral import track without silently choosing a close runner-up."""
    results = with_retry(lambda: target_client.search(f"{source.title} {source.artist}", "songs")) or []
    want = normalize(f"{source.title} {source.artist}")
    candidates = []
    for row in results:
        video_id = row.get("videoId")
        if not video_id:
            continue
        score = fuzzy_ratio(want, normalize(f"{row.get('title', '')} {track_artist(row)}"))
        if score < fuzzy_threshold:
            continue
        duration = row.get("duration_seconds")
        within = (source.duration_s is not None and duration is not None
                  and abs(duration - source.duration_s) <= 3)
        if not within and score < .95:
            continue
        candidates.append((within, score, video_id, row.get("title") or "", track_artist(row)))
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    if not candidates:
        return ImportResolution(source, "unmatched", None, None, None, None)
    best = candidates[0]
    # Two similarly convincing recordings require a user choice; taking API order would be silent
    # substitution. Duration agreement is the primary tier, then normalized title/artist score.
    if len(candidates) > 1:
        second = candidates[1]
        if best[0] == second[0] and best[1] - second[1] < ambiguity_gap:
            return ImportResolution(source, "ambiguous", None, best[3], best[4], best[1])
    return ImportResolution(source, "matched", best[2], best[3], best[4], best[1])


def existing_overlaps(store, target_identity, rows, threshold=.70) -> list[ExistingOverlap]:
    """Compare a virtual imported playlist with manageable playlists in one destination identity.

    Exact means every source row resolved confidently *and* canonical song sets are equal. This
    prevents a partial import from being called a duplicate merely because its known subset matches.
    """
    source_keys = {identity_key(r.source.title, r.source.artist) for r in rows}
    fully_resolved = bool(rows) and all(r.status == "matched" for r in rows)
    excluded = store.excluded_playlist_ids()
    out = []
    for playlist in store.get_playlists():
        if (playlist.identity_id != target_identity or playlist.id in excluded
                or playlist.ytm_playlist_id in SYSTEM_PLAYLIST_IDS):
            continue
        current = store.get_playlist_track_keys(playlist.id)
        similarity = jaccard(source_keys, current)
        if similarity < threshold:
            continue
        shared = source_keys & current
        out.append(ExistingOverlap(
            playlist.id, playlist.title, similarity,
            fully_resolved and source_keys == current,
            len(shared), len(source_keys - current), len(current - source_keys)))
    return sorted(out, key=lambda row: (row.exact, row.similarity), reverse=True)
