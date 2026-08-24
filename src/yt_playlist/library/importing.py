"""Provider-neutral target matching for one-shot playlist imports."""
from dataclasses import dataclass

from yt_playlist.util.matching import fuzzy_ratio, normalize, track_artist
from yt_playlist.util.retry import with_retry


@dataclass(frozen=True)
class ImportResolution:
    source: object
    status: str                    # matched | ambiguous | unmatched
    target_video_id: str | None
    target_title: str | None
    target_artist: str | None
    score: float | None


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
