"""Local recommendation logic. Pure functions over a Store (no web imports), like analysis.py."""
from dataclasses import dataclass

import math
import statistics

from yt_playlist import analysis, embed, genre_map
from yt_playlist.rec_dao import RecDao


def taste_breadth(store) -> dict:
    """How narrow vs eclectic this library is, from the entropy of its genre-family mix.

    breadth in [0,1]: ~0 = one-vibe (opera-only), ~1 = spread across many families. Computed
    over tagged tracks (genre is sparse today, so it sharpens as enrichment fills). Spec §5.2.
    """
    fams: dict = {}
    for genre, c in store.genre_distribution().items():
        fams[genre_map.family(genre)] = fams.get(genre_map.family(genre), 0) + c
    total = sum(fams.values())
    if total == 0 or len(fams) <= 1:
        return {"breadth": 0.0, "n_families": len(fams),
                "families": {f: 1.0 for f in fams}, "n_tagged": total}
    shares = {f: c / total for f, c in fams.items()}
    entropy = -sum(p * math.log(p) for p in shares.values())
    return {"breadth": entropy / math.log(len(fams)), "n_families": len(fams),
            "families": shares, "n_tagged": total}


def palette(store):
    """The library's genre-family palette + an out-of-palette penalty for a candidate genre.

    fit(genre) = 0 if its family is already present; otherwise the nearest-present-family
    distance scaled by breadth — 'absence-as-avoidance': a broad library that has never adopted
    a family penalizes it more (deliberate exclusion), a narrow one less (merely unexplored).
    Spec §5.3.
    """
    bd = taste_breadth(store)
    present = set(bd["families"])

    def fit(genre):
        fam = genre_map.family(genre)
        if not present or fam in present:
            return 0.0
        nearest = min(genre_map.family_distance(fam, p) for p in present)
        return nearest * (0.5 + bd["breadth"])

    return {"breadth": bd["breadth"], "present": present, "fit": fit}

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
    key: str = ""      # track identity_key, for feedback (dismiss/less/mute)
    lane: str = ""     # source lane (resurface/neighbourhood/rotation/deep_cut), for weighting


def for_you(store, now, limit=24) -> list[ForYouItem]:
    """Blended local recommendations, interleaved from several real signals and deduped.

    Sources, strongest-available first:
      - forgotten gems: songs you played a lot but not in the recent window (grows with history)
      - rotation neighbours: songs that share playlists with your most-played, that you barely play
      - deep cuts: the most-neglected track of each artist you play a lot
    """
    pool = limit * 4   # fetch deeper than we show, so erosion has inventory to rotate through
    sources = [
        (store.resurface_candidates(now, limit=pool),
         lambda r: "You played this a lot — give it another spin", "resurface"),
    ]
    # the taste-embedding lane: tracks in the neighbourhood of what you play most.
    # Falls back to the plain co-occurrence query until the model has been built.
    nbrs = _taste_neighbourhood(store, pool, now) if store.rec_vectors_count() else None
    if nbrs:
        sources.append((nbrs, lambda r: "In your taste neighbourhood", "neighbourhood"))
    else:
        sources.append((store.more_like_rotation(limit=pool),
                        lambda r: _rotation_reason(r["shared_playlists"]), "rotation"))
    sources.append((store.deep_cuts(limit=pool),
                    lambda r: f"A deep cut from {r['artist']}, who you play a lot", "deep_cut"))

    # Tier-2 refinement: re-rank every lane's candidates by similarity to your taste centroid
    # (slow all-time + fast recent), so the strongest-fitting items rise within each lane.
    if store.rec_vectors_count():
        slow = store.top_played_keys(limit=8)
        recent = list(store.get_recent_history_keys(now - 86400.0))[:12]
        groups = [(slow, 0.6)] + ([(recent, 0.4)] if recent else [])
        all_keys = [r["key"] for rows, _, _ in sources for r in rows]
        sims = embed.sims_for(store, groups, all_keys)
        if sims:
            for rows, _, _ in sources:
                rows.sort(key=lambda r: -sims.get(r["key"], -1.0))

    weights = store.get_weights()
    # suppress dismissed/snoozed/muted, plus eroded items (shown enough lately) — anti-staleness
    suppressed = store.suppressed_keys("for_you", now) | RecDao(store).eroded_keys("for_you", now)
    muted = store.muted_artists()
    # weighted fair queuing: each lane gets turns ∝ its learned weight (default 1.0 => round-robin)
    queues = [[list(rows), reason, lane, weights.get(f"lane:{lane}", 1.0), 0]
              for rows, reason, lane in sources]
    seen: set = set()
    out: list[ForYouItem] = []
    while len(out) < limit:
        live = [q for q in queues if q[0]]
        if not live:
            break
        q = max(live, key=lambda q: q[3] / (q[4] + 1))   # weight / (taken + 1)
        rows, reason, lane, _, _ = q
        r = rows.pop(0)
        if r["key"] in seen or r["key"] in suppressed or r["artist"] in muted:
            seen.add(r["key"])
            continue
        seen.add(r["key"])
        q[4] += 1
        out.append(ForYouItem(
            title=r["title"], artist=r["artist"], album=r["album"], video_id=r["video_id"],
            thumbnail=r["thumbnail"], plays=r["plays"], reason=reason(r), key=r["key"], lane=lane))
    return out


def _rotation_reason(n) -> str:
    return f"Sits with your favorites in {n} of your playlist{'s' if n != 1 else ''}"


def new_albums_from_favorites(ctx, limit_artists=6, limit=12) -> list[dict]:
    """Outward discovery (Phase 2): albums by your most-played artists that you don't already own
    or have saved. Uses the YTM client (network) — meant to run in the background worker, not per
    request. Degrades to [] with no client/network. Spec §1/§8 discovery."""
    from yt_playlist.web.routes.charts import _fetch_artist_info   # reuse the existing fetch
    store = ctx.store
    dao = RecDao(store)
    owned, saved = dao.owned_albums(), dao.saved_album_ids()
    out: list[dict] = []
    for a in store.top_artists(limit_artists):
        info = _fetch_artist_info(ctx, a["artist"])
        if not info:
            continue
        for alb in info.get("albums") or []:
            title = (alb.get("title") or "").strip()
            if not title or title.lower() in owned or alb.get("browse_id") in saved:
                continue
            out.append({"artist": a["artist"], "title": title, "year": alb.get("year"),
                        "browse_id": alb.get("browse_id"), "thumbnail": alb.get("thumbnail")})
            if len(out) >= limit:
                return out
    return out


def fresh_songs(ctx, limit=10) -> list[dict]:
    """Outward (Phase 2): songs from YTM radios seeded by your top tracks that you don't own.

    Uses the client (network) — runs in the background worker, never per request. Degrades to []
    with no client/network. Spec §8 '10 fresh songs not in your library'."""
    from yt_playlist.matching import identity_key
    from yt_playlist.thumbnails import best_thumb
    client = next(iter((ctx.client_provider() or {}).values()), None)
    if client is None:
        return []
    owned = RecDao(ctx.store).library_keys()
    out, seen = [], set()
    for t in ctx.store.top_tracks(6):
        vid = t.get("video_id")
        if not vid:
            continue
        try:
            radio = client.get_watch_playlist(vid) or {}
        except Exception:  # noqa: BLE001 - network/parse/missing-method -> skip this seed
            continue
        for r in radio.get("tracks") or []:
            v, title = r.get("videoId"), (r.get("title") or "").strip()
            artist = ((r.get("artists") or [{}])[0] or {}).get("name", "")
            if not v or not title:
                continue
            key = identity_key(title, artist)
            if key in owned or key in seen:
                continue
            seen.add(key)
            out.append({"video_id": v, "title": title, "artist": artist,
                        "thumbnail": best_thumb(r.get("thumbnails"))})
            if len(out) >= limit:
                return out
    return out


def auto_playlists(store, k=16, min_size=10, max_proposals=6) -> list[dict]:
    """Cluster the taste-embedding space into coherent groups and propose the ones that aren't
    already a playlist. Each proposal: {label, size, keys, sample}. Spec §8."""
    clusters = embed.cluster(store, k)
    if not clusters:
        return []
    existing = [set(store.get_playlist_track_keys(p.id)) for p in store.get_playlists()]
    existing = [e for e in existing if e]
    dao = RecDao(store)
    props = []
    for keys in clusters.values():
        if len(keys) < min_size:
            continue
        ks = set(keys)
        if any(len(ks & e) / len(ks) > 0.6 for e in existing):   # already basically a playlist
            continue
        meta = store.tracks_by_keys(keys)
        props.append({
            "label": _cluster_label(dao, keys, meta),
            "size": len(keys),
            "keys": list(ks),
            "sample": [meta[k] for k in keys if k in meta][:6],
        })
    props.sort(key=lambda p: -p["size"])
    return props[:max_proposals]


def _cluster_label(dao, keys, meta):
    """Name a cluster by its dominant genre family (if tagged) plus a couple of artists."""
    fams: dict = {}
    for g in dao.track_genres(keys).values():
        fam = genre_map.family(g)
        if not fam.startswith("other:"):
            fams[fam] = fams.get(fam, 0) + 1
    artists = {}
    for k in keys:
        a = (meta.get(k) or {}).get("artist")
        if a:
            artists[a] = artists.get(a, 0) + 1
    top_artists = [a for a, _ in sorted(artists.items(), key=lambda x: -x[1])[:2]]
    fam = max(fams, key=fams.get).replace("-", " ").title() if fams else "Mixed"
    return f"{fam} · incl. {', '.join(top_artists)}" if top_artists else fam


def explore_for_you(store, now, limit=24) -> list[ForYouItem]:
    """The 'try something new' lane: tracks near your taste but novel — sitting close to your
    centroid yet by artists you *don't* play much. The edge of your palette, not the centre.
    Empty until the embedding model is built. Spec §5.4/§5.5.
    """
    if not store.rec_vectors_count():
        return []
    seeds = store.top_played_keys(limit=8)
    if not seeds:
        return []
    nbrs = embed.centroid_neighbors(store, seeds, topn=limit * 12, exclude=set(seeds))
    if not nbrs:
        return []
    meta = store.tracks_by_keys([k for k, _ in nbrs])
    familiar = {a["artist"] for a in store.top_artists(25)}     # artists you already play
    suppressed = store.suppressed_keys("for_you", now) | RecDao(store).eroded_keys("explore", now)
    muted = store.muted_artists()
    out: list[ForYouItem] = []
    for k, _ in nbrs:
        m = meta.get(k)
        if not m or k in suppressed or m["artist"] in muted or m["artist"] in familiar:
            continue
        out.append(ForYouItem(m["title"], m["artist"], m["album"], m["video_id"], m["thumbnail"],
                              0, "New to you — sits near your taste", k, "explore"))
        if len(out) >= limit:
            break
    return out


def _taste_neighbourhood(store, limit, now=None):
    """Embedding-based: tracks near a blend of your *all-time* taste (slow) and your *recent*
    plays (fast/mood). Recent listening tilts the lane toward your current vibe. Spec §5.1."""
    slow = store.top_played_keys(limit=8)
    if not slow:
        return None
    groups = [(slow, 0.6)]
    if now is not None:
        recent = list(store.get_recent_history_keys(now - 86400.0))[:12]   # last day = mood
        if recent:
            groups.append((recent, 0.4))
    nbrs = embed.blended_neighbors(store, groups, topn=limit, exclude=set(slow))
    if not nbrs:
        return None
    meta = store.tracks_by_keys([k for k, _ in nbrs])
    return [{"key": k, "plays": 0, **meta[k]} for k, _ in nbrs if k in meta]


def complete_playlist(store, playlist_id, limit=12, now=None) -> list[ForYouItem]:
    """Tracks you own that fit a given playlist but aren't in it yet.

    Uses the taste-embedding model (nearest to the playlist's centroid) once it's built;
    falls back to the artist/co-occurrence heuristic until then.
    """
    members = store.get_playlist_track_keys(playlist_id)
    scope = str(playlist_id)
    suppressed = store.suppressed_keys("suggest", now or 0, scope=scope)
    muted = store.muted_artists()

    def keep(key, artist):
        return key not in suppressed and artist not in muted

    if store.rec_vectors_count() and members:
        nbrs = embed.centroid_neighbors(store, list(members), topn=limit * 2, exclude=members)
        if nbrs:
            meta = store.tracks_by_keys([k for k, _ in nbrs])
            member_artists = {m["artist"] for m in store.tracks_by_keys(members).values()}
            out = []
            for k, _ in nbrs:
                m = meta.get(k)
                if not m or not keep(k, m["artist"]):
                    continue
                reason = (f"More from {m['artist']}, already here" if m["artist"] in member_artists
                          else "Matches the sound of this playlist")
                out.append(ForYouItem(m["title"], m["artist"], m["album"], m["video_id"],
                                      m["thumbnail"], 0, reason, k))
                if len(out) >= limit:
                    break
            return out

    out: list[ForYouItem] = []
    for r in store.complete_playlist(playlist_id, limit=limit * 2):
        if not keep(r["key"], r["artist"]):
            continue
        if r["same_artist"] and r["cooc"]:
            reason = f"By {r['artist']} (already here), and in {r['cooc']} related playlist(s)"
        elif r["same_artist"]:
            reason = f"More from {r['artist']}, already in this playlist"
        else:
            reason = f"Sits with these tracks in {r['cooc']} of your playlists"
        out.append(ForYouItem(
            title=r["title"], artist=r["artist"], album=r["album"], video_id=r["video_id"],
            thumbnail=r["thumbnail"], plays=0, reason=reason, key=r["key"]))
        if len(out) >= limit:
            break
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
    key: str = ""      # stable id for dismiss/snooze (e.g. 'enrich:12', 'cleanup:empty')


def _ago(seconds) -> str:
    days = int(seconds // 86400)
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''} ago"
    hours = int(seconds // 3600)
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return "just now"


def take_action(store, now, auth_expired) -> list[ActionItem]:
    """Cards for things that genuinely need attention. Empty list = render nothing.

    Honors per-card snooze: an alert dismissed by the user stays hidden until its cooldown.
    """
    snoozed = store.suppressed_keys("alert", now)
    items: list[ActionItem] = []
    for label in auth_expired.values():
        items.append(ActionItem(
            "auth", "high", f"Re-authenticate {label}",
            "YouTube session expired — sync and recommendations are stale until you reconnect.",
            "Re-authenticate", "/setup", key=f"auth:{label}"))

    empties = analysis.find_empty_playlists(store)
    if empties:
        items.append(ActionItem(
            "cleanup", "low", f"{len(empties)} empty playlist(s)",
            "Empty playlists clutter your library — review and remove them.",
            "Review", "/cleanup", key="cleanup:empty"))

    dupes = analysis.find_near_duplicate_groups(store)
    if dupes:
        items.append(ActionItem(
            "cleanup", "low", f"{len(dupes)} near-duplicate group(s)",
            "Some playlists heavily overlap — review for merges.",
            "Review", "/cleanup", key="cleanup:dupes"))

    for e in store.enrichment_candidates(limit=3):
        items.append(ActionItem(
            "enrich", "low", f'Enrich "{e["title"]}"',
            f"{e['gaps']} of {e['total']} tracks are missing genre tags — and it's one of your "
            f"most-played playlists ({e['plays']} plays). Enriching it sharpens recommendations, "
            "since recs lean on genre and year.",
            "Enrich", f"/playlist/{e['id']}", thumbnail=e["thumbnail"], key=f"enrich:{e['id']}"))

    return [i for i in items if i.key not in snoozed]
