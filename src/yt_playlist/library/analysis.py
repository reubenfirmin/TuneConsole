from dataclasses import dataclass
from itertools import combinations
from yt_playlist.core.store import Playlist

@dataclass
class DupeFinding:
    playlist_a: Playlist; playlist_b: Playlist; similarity: float
    shared: set; only_a: set; only_b: set

    @property
    def identical(self) -> bool:
        """Same set of tracks in both playlists — the clean fix is to delete one."""
        return not self.only_a and not self.only_b

@dataclass
class OverlapFinding:
    playlist_a: Playlist; playlist_b: Playlist; shared: set
    count_a: int = 0; count_b: int = 0

    def pct_a(self) -> int:
        return round(len(self.shared) / self.count_a * 100) if self.count_a else 0

    def pct_b(self) -> int:
        return round(len(self.shared) / self.count_b * 100) if self.count_b else 0

# YouTube system playlists (can't be deleted, not real user playlists): Liked Music, Episodes for Later
SYSTEM_PLAYLIST_IDS = {"LM", "SE"}

def _manageable(p) -> bool:
    return p.ytm_playlist_id not in SYSTEM_PLAYLIST_IDS

@dataclass
class IdenticalGroup:
    playlists: list   # all playlists sharing the exact same track set (2+)
    track_count: int  # number of tracks each copy has

def merge_signature(ytms) -> str:
    """Canonical, membership-only id for a merge suggestion (an exact/near-dup GROUP): the member
    ytm playlist ids, sorted and joined. Stable across re-sync as long as the cluster's membership is
    unchanged; a member added/removed yields a different signature, so the merge re-surfaces as the
    genuinely different suggestion it now is. Used to dismiss 'this merge' without touching the
    individual playlists' other relationships."""
    return "|".join(sorted(ytms))


def _group_sig(playlists) -> str:
    return merge_signature(p.ytm_playlist_id for p in playlists)


def find_identical_groups(store, ignored_sigs=None):
    """Cluster playlists by identical track set so N copies show as one group, not N-choose-2 pairs.

    Empty playlists are excluded (they aren't really "the same playlist" — see find_empty_playlists).
    `ignored_sigs`: merge signatures the user dismissed — those groups are hidden.
    """
    ignored_sigs = ignored_sigs or set()
    by_keys = {}
    for p in store.get_playlists():
        if not _manageable(p):
            continue
        keys = frozenset(store.get_playlist_track_keys(p.id))
        if not keys:            # empty -> not a real duplicate; handled separately
            continue
        by_keys.setdefault(keys, []).append(p)
    groups = [IdenticalGroup(sorted(pls, key=lambda x: x.id), len(keys))
              for keys, pls in by_keys.items() if len(pls) > 1]
    groups = [g for g in groups if _group_sig(g.playlists) not in ignored_sigs]   # drop dismissed merges
    # biggest clusters first, then most tracks
    return sorted(groups, key=lambda g: (len(g.playlists), g.track_count), reverse=True)

@dataclass
class NearGroup:
    playlists: list      # connected cluster of mutually near-duplicate playlists (2+)
    avg_similarity: float

def find_near_duplicate_groups(store, threshold=0.70, exclude_playlist_ids=None, ignored_sigs=None):
    """Cluster near-duplicates (similar but not identical) into connected components, so N related
    playlists show as one group instead of N-choose-2 pairwise rows. Identical playlists are handled
    by find_identical_groups and excluded here.

    `ignored_sigs`: merge signatures the user dismissed — those groups are hidden."""
    exclude = exclude_playlist_ids or set()
    ignored_sigs = ignored_sigs or set()
    edges = [d for d in find_dupes(store, threshold)
             if not d.identical
             and d.playlist_a.id not in exclude and d.playlist_b.id not in exclude]
    adj, sims = {}, {}
    for d in edges:
        a, b = d.playlist_a.id, d.playlist_b.id
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
        sims[frozenset((a, b))] = d.similarity
    pls = {p.id: p for p in store.get_playlists()}
    seen, groups = set(), []
    for start in adj:
        if start in seen:
            continue
        comp, stack = set(), [start]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n); comp.add(n)
            stack.extend(adj[n] - seen)
        if len(comp) < 2:
            continue
        members = sorted((pls[i] for i in comp if i in pls), key=lambda p: p.id)
        if _group_sig(members) in ignored_sigs:        # this merge was dismissed
            continue
        inner = [s for pair, s in sims.items() if pair <= comp]
        groups.append(NearGroup(members, sum(inner) / len(inner) if inner else 0.0))
    return sorted(groups, key=lambda g: len(g.playlists), reverse=True)

def find_empty_playlists(store, ignored=None):
    """User playlists with no tracks (excludes undeletable system playlists like Liked Music).
    `ignored`: ytm ids dismissed from the Empty category."""
    ignored = ignored or set()
    return sorted((p for p in store.get_playlists()
                   if _manageable(p) and p.ytm_playlist_id not in ignored
                   and not store.get_playlist_track_keys(p.id)),
                  key=lambda p: p.title.lower())

def find_tiny_playlists(store, max_tracks=3, ignored=None):
    """Manageable playlists with 1..max_tracks tracks — candidates to merge away or prune.
    `ignored`: ytm ids dismissed from the Tiny category."""
    ignored = ignored or set()
    out = []
    for p in store.get_playlists():
        if not _manageable(p) or p.ytm_playlist_id in ignored:
            continue
        n = len(store.get_playlist_track_keys(p.id))
        if 1 <= n <= max_tracks:
            out.append((n, p))
    return [p for _, p in sorted(out, key=lambda np: (np[0], np[1].title.lower()))]

def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)

def _pairs_with_keys(store):
    # system playlists (Liked Music, Episodes for Later) are never dupe/overlap candidates
    pls = [p for p in store.get_playlists() if _manageable(p)]
    keyed = [(p, store.get_playlist_track_keys(p.id)) for p in pls]
    for (pa, ka), (pb, kb) in combinations(keyed, 2):
        yield pa, ka, pb, kb

def find_dupes(store, threshold=0.70):
    out = []
    for pa, ka, pb, kb in _pairs_with_keys(store):
        sim = jaccard(ka, kb)
        if sim >= threshold:
            out.append(DupeFinding(pa, pb, sim, ka & kb, ka - kb, kb - ka))
    return sorted(out, key=lambda f: f.similarity, reverse=True)

def find_overlaps(store, dupe_threshold=0.70, exclude_playlist_ids=None, suppressed=None,
                  ignored_ytm=None, kept=None):
    # exclude_playlist_ids: playlists already shown as duplicates — hide them here so the same
    # playlist doesn't appear in both sections (resolve dupes first; the rest resurface on re-sync).
    # suppressed: set of frozenset({ytm_a, ytm_b}) pairs the user has manually hidden.
    # ignored_ytm: playlists the user excludes from overlap detection entirely (e.g. a huge mixtape).
    # kept: pairs to ALWAYS keep visible even if one side is ignored (the "keep this pair" exception).
    exclude = exclude_playlist_ids or set()
    suppressed = suppressed or set()
    ignored_ytm = ignored_ytm or set()
    kept = kept or set()
    out = []
    for pa, ka, pb, kb in _pairs_with_keys(store):
        pair = frozenset((pa.ytm_playlist_id, pb.ytm_playlist_id))
        if pa.id in exclude or pb.id in exclude:
            continue
        if pair not in kept and (pa.ytm_playlist_id in ignored_ytm or pb.ytm_playlist_id in ignored_ytm):
            continue
        if pair in suppressed:
            continue
        shared = ka & kb
        if shared and jaccard(ka, kb) < dupe_threshold:
            out.append(OverlapFinding(pa, pb, shared, len(ka), len(kb)))
    return sorted(out, key=lambda f: len(f.shared), reverse=True)

@dataclass
class CleanupSummary:
    playlists: list   # distinct, non-ignored playlists across all cleanup categories (dupes first)

    @property
    def count(self) -> int:
        return len(self.playlists)

    def thumbnails(self, n=2) -> list:
        """Cover URLs of the first `n` involved playlists that have art (for the home alert card)."""
        return [p.thumbnail for p in self.playlists if p.thumbnail][:n]

    def as_payload(self) -> dict:
        """The tiny, JSON-serializable shape the home card needs — cached as a rec proposal so the
        home page never re-runs the (O(n²)) scan below on every load. See recommend.refresh_cleanup."""
        return {"count": self.count, "thumbnails": self.thumbnails(2)}

def cleanup_summary(store):
    """Collapse everything the /cleanup page surfaces into one 'is there anything to tidy?' answer:
    the distinct playlists involved across all cleanup categories (exact + near duplicates, empties,
    tiny playlists, and overlaps), minus any the user has ignored. Mirrors the cleanup page's own
    exclusions so a fully-triaged library reports nothing. Insertion order is dupes-first, so the
    thumbnails it surfaces are the playlists most likely to have cover art."""
    ci = store.get_cleanup_ignored()                    # {category: set(ytm)} per-playlist dismissals
    ignored_sigs = store.get_ignored_merge_sigs()       # dismissed merge suggestions
    groups = find_identical_groups(store, ignored_sigs=ignored_sigs)
    exact_ids = {p.id for g in groups for p in g.playlists}
    near_groups = find_near_duplicate_groups(store, exclude_playlist_ids=exact_ids,
                                             ignored_sigs=ignored_sigs)
    dupe_ids = {p.id for d in find_dupes(store) for p in (d.playlist_a, d.playlist_b)}
    overlaps = find_overlaps(store, exclude_playlist_ids=dupe_ids,
                             suppressed=store.get_suppressed_overlap_pairs(),
                             ignored_ytm=store.get_overlap_ignored(), kept=store.get_overlap_kept_pairs())
    # Each category already excludes its own dismissals, so the union is simply what's still pending.
    involved = {}
    for g in (*groups, *near_groups):
        for p in g.playlists:
            involved.setdefault(p.id, p)
    for p in (*find_empty_playlists(store, ignored=ci.get("empty")),
              *find_tiny_playlists(store, ignored=ci.get("tiny"))):
        involved.setdefault(p.id, p)
    for o in overlaps:
        involved.setdefault(o.playlist_a.id, o.playlist_a)
        involved.setdefault(o.playlist_b.id, o.playlist_b)
    return CleanupSummary(list(involved.values()))

