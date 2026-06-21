"""Local recommendation logic. Pure functions over a Store (no web imports), like analysis.py."""
from dataclasses import dataclass

import statistics

from yt_playlist import analysis, embed, genre_map

SYNC_STALE_S = 24 * 3600   # highlight the Sync card after 24h


def playlist_genre_diversity(store, playlist_id):
    """How genre-tight vs genre-wide a playlist is, via pairwise genre-map distances.

    Returns {min, max, median, n_tagged} over its tagged tracks, or None if fewer than two
    are tagged. median≈0 = tight (one vibe); median≈1 = eclectic. Spec §6.B.
    """
    genres = store.playlist_track_genres(playlist_id)
    if len(genres) < 2:
        return None
    dists = [genre_map.distance(genres[i], genres[j])
             for i in range(len(genres)) for j in range(i + 1, len(genres))]
    return {"min": min(dists), "max": max(dists),
            "median": statistics.median(dists), "n_tagged": len(genres)}


def genre_distance_fn(store, alpha=0.5):
    """A genre-distance function blending the static meta-genre map with this library's own
    co-occurrence: genres you repeatedly playlist together are pulled closer. alpha = static
    weight. Falls back to the static map for pairs you've never grouped. Spec §2.1/§5.3.
    """
    co = store.genre_cooccurrence()
    pairs, occ = co["pairs"], co["occ"]

    def dist(g1, g2):
        base = genre_map.distance(g1, g2)
        a, b = (g1, g2) if g1 <= g2 else (g2, g1)
        c = pairs.get((a, b), 0)
        if c == 0 or not occ.get(g1) or not occ.get(g2):
            return base
        jaccard = c / (occ[g1] + occ[g2] - c)
        return alpha * base + (1 - alpha) * (1 - jaccard)

    return dist


@dataclass
class ForYouItem:
    title: str
    artist: str
    album: str
    video_id: str | None
    thumbnail: str | None
    plays: int
    reason: str        # why this was recommended (human-readable)


def for_you(store, now, limit=24) -> list[ForYouItem]:
    """Blended local recommendations, interleaved from several real signals and deduped.

    Sources, strongest-available first:
      - forgotten gems: songs you played a lot but not in the recent window (grows with history)
      - rotation neighbours: songs that share playlists with your most-played, that you barely play
      - deep cuts: the most-neglected track of each artist you play a lot
    """
    sources = [
        (store.resurface_candidates(now, limit=limit),
         lambda r: "You played this a lot — give it another spin"),
    ]
    # the taste-embedding lane: tracks in the neighbourhood of what you play most.
    # Falls back to the plain co-occurrence query until the model has been built.
    nbrs = _taste_neighbourhood(store, limit) if store.rec_vectors_count() else None
    if nbrs:
        sources.append((nbrs, lambda r: "In your taste neighbourhood"))
    else:
        sources.append((store.more_like_rotation(limit=limit),
                        lambda r: _rotation_reason(r["shared_playlists"])))
    sources.append((store.deep_cuts(limit=limit),
                    lambda r: f"A deep cut from {r['artist']}, who you play a lot"))
    queues = [(list(rows), reason) for rows, reason in sources]
    seen: set = set()
    out: list[ForYouItem] = []
    i = 0
    # round-robin across sources for variety; stop when full or every source is drained
    while len(out) < limit and any(rows for rows, _ in queues):
        rows, reason = queues[i % len(queues)]
        i += 1
        while rows:
            r = rows.pop(0)
            if r["key"] in seen:
                continue
            seen.add(r["key"])
            out.append(ForYouItem(
                title=r["title"], artist=r["artist"], album=r["album"], video_id=r["video_id"],
                thumbnail=r["thumbnail"], plays=r["plays"], reason=reason(r)))
            break
    return out


def _rotation_reason(n) -> str:
    return f"Sits with your favorites in {n} of your playlist{'s' if n != 1 else ''}"


def _taste_neighbourhood(store, limit):
    """Embedding-based: tracks near the centroid of your most-played songs."""
    seeds = store.top_played_keys(limit=8)
    nbrs = embed.centroid_neighbors(store, seeds, topn=limit, exclude=set(seeds))
    if not nbrs:
        return None
    meta = store.tracks_by_keys([k for k, _ in nbrs])
    return [{"key": k, "plays": 0, **meta[k]} for k, _ in nbrs if k in meta]


def complete_playlist(store, playlist_id, limit=12) -> list[ForYouItem]:
    """Tracks you own that fit a given playlist but aren't in it yet.

    Uses the taste-embedding model (nearest to the playlist's centroid) once it's built;
    falls back to the artist/co-occurrence heuristic until then.
    """
    members = store.get_playlist_track_keys(playlist_id)
    if store.rec_vectors_count() and members:
        nbrs = embed.centroid_neighbors(store, list(members), topn=limit, exclude=members)
        if nbrs:
            meta = store.tracks_by_keys([k for k, _ in nbrs])
            member_artists = {m["artist"] for m in store.tracks_by_keys(members).values()}
            out = []
            for k, _ in nbrs:
                m = meta.get(k)
                if not m:
                    continue
                reason = (f"More from {m['artist']}, already here" if m["artist"] in member_artists
                          else "Matches the sound of this playlist")
                out.append(ForYouItem(m["title"], m["artist"], m["album"], m["video_id"],
                                      m["thumbnail"], 0, reason))
            return out

    out: list[ForYouItem] = []
    for r in store.complete_playlist(playlist_id, limit=limit):
        if r["same_artist"] and r["cooc"]:
            reason = f"By {r['artist']} (already here), and in {r['cooc']} related playlist(s)"
        elif r["same_artist"]:
            reason = f"More from {r['artist']}, already in this playlist"
        else:
            reason = f"Sits with these tracks in {r['cooc']} of your playlists"
        out.append(ForYouItem(
            title=r["title"], artist=r["artist"], album=r["album"], video_id=r["video_id"],
            thumbnail=r["thumbnail"], plays=0, reason=reason))
    return out


@dataclass
class SyncStatus:
    last_synced_ago: str | None   # None if never synced
    stale: bool                   # never synced, or older than SYNC_STALE_S
    message: str | None           # highlight copy when stale, else None


def sync_status(store, now) -> SyncStatus:
    last = store.get_setting("last_sync_at")
    if last is None:
        return SyncStatus(None, True, "Sync to pull in your library and recommendations.")
    age = now - float(last)
    if age > SYNC_STALE_S:
        return SyncStatus(_ago(age), True, "It's been a while — sync to refresh.")
    return SyncStatus(_ago(age), False, None)


@dataclass
class ActionItem:
    kind: str          # "auth" | "cleanup" | "enrich"
    severity: str      # "high" | "med" | "low"
    title: str
    detail: str
    cta_label: str | None
    cta_href: str | None
    thumbnail: str | None = None


def _ago(seconds) -> str:
    days = int(seconds // 86400)
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''} ago"
    hours = int(seconds // 3600)
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return "just now"


def take_action(store, now, auth_expired) -> list[ActionItem]:
    """Cards for things that genuinely need attention. Empty list = render nothing."""
    items: list[ActionItem] = []
    for label in auth_expired.values():
        items.append(ActionItem(
            "auth", "high", f"Re-authenticate {label}",
            "YouTube session expired — sync and recommendations are stale until you reconnect.",
            "Re-authenticate", "/setup"))

    empties = analysis.find_empty_playlists(store)
    if empties:
        items.append(ActionItem(
            "cleanup", "low", f"{len(empties)} empty playlist(s)",
            "Empty playlists clutter your library — review and remove them.",
            "Review", "/cleanup"))

    dupes = analysis.find_near_duplicate_groups(store)
    if dupes:
        items.append(ActionItem(
            "cleanup", "low", f"{len(dupes)} near-duplicate group(s)",
            "Some playlists heavily overlap — review for merges.",
            "Review", "/cleanup"))

    for e in store.enrichment_candidates(limit=3):
        items.append(ActionItem(
            "enrich", "low", f'Enrich "{e["title"]}"',
            f"{e['gaps']} of {e['total']} tracks are missing genre tags — and it's one of your "
            f"most-played playlists ({e['plays']} plays). Enriching it sharpens recommendations, "
            "since recs lean on genre and year.",
            "Enrich", f"/playlist/{e['id']}", thumbnail=e["thumbnail"]))

    return items
