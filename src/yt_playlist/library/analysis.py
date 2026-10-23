from dataclasses import dataclass
from contextlib import nullcontext
from collections import Counter, defaultdict
from itertools import combinations
from yt_playlist.repos.models import Playlist, PlaylistMemberships, CleanupPreferences

@dataclass
class OverlapFinding:
    playlist_a: Playlist; playlist_b: Playlist
    keys_a: frozenset; keys_b: frozenset; shared_count: int

    @property
    def shared(self):
        return self.keys_a & self.keys_b

    @property
    def count_a(self):
        return len(self.keys_a)

    @property
    def count_b(self):
        return len(self.keys_b)

    def pct_a(self) -> int:
        return round(self.shared_count / self.count_a * 100) if self.count_a else 0

    def pct_b(self) -> int:
        return round(self.shared_count / self.count_b * 100) if self.count_b else 0


@dataclass
class DupeFinding(OverlapFinding):
    similarity: float

    @property
    def only_a(self):
        return self.keys_a - self.keys_b

    @property
    def only_b(self):
        return self.keys_b - self.keys_a

    @property
    def identical(self) -> bool:
        return self.shared_count == self.count_a == self.count_b

# YouTube system playlists (can't be deleted, not real user playlists): Liked Music, Episodes for Later
SYSTEM_PLAYLIST_IDS = {"LM", "SE"}

def _manageable(p) -> bool:
    return p.ytm_playlist_id not in SYSTEM_PLAYLIST_IDS

class CleanupAnalysis:
    """Pure, operation-scoped analysis of detached membership/preferences.

    Relationships are partitioned once per threshold. Category projections and
    the Home summary reuse them without querying persistence or comparing sets.
    """
    def __init__(self, memberships: PlaylistMemberships, preferences: CleanupPreferences):
        self.memberships = memberships
        self.preferences = preferences
        self.playlists = [p for p in memberships.playlists
                          if _manageable(p) and p.id not in memberships.excluded]
        self._partitions = {}

    def _candidate_pairs(self, threshold):
        """Count intersections through recording postings, grouping identical sets first.

        Only the output can be quadratic for dense overlap. No shared/difference sets are
        retained per pair. Nonpositive thresholds deliberately include even disjoint/empty
        pairs (their historical similarity is zero), so those requests use all pairs.
        """
        keys = self.memberships.keys
        if threshold <= 0:
            for a, b in combinations(range(len(self.playlists)), 2):
                yield a, b, len(keys[self.playlists[a].id] & keys[self.playlists[b].id])
            return
        grouped = {}
        for index, playlist in enumerate(self.playlists):
            members = keys[playlist.id]
            if members:
                grouped.setdefault(members, []).append(index)
        groups = list(grouped.items())
        postings = defaultdict(list)
        for group, (members, _) in enumerate(groups):
            for key in members:
                postings[key].append(group)
        for group, (members, indices) in enumerate(groups):
            for a, b in combinations(indices, 2):
                yield a, b, len(members)
            shared = Counter()
            for key in members:
                for other in postings[key]:
                    if other > group:
                        shared[other] += 1
            for other, count in shared.items():
                for a in indices:
                    for b in groups[other][1]:
                        yield min(a, b), max(a, b), count

    def relationships(self, threshold):
        if threshold not in self._partitions:
            dupes, overlaps = [], []
            for a, b, count in self._candidate_pairs(threshold):
                pa, pb = self.playlists[a], self.playlists[b]
                ka, kb = self.memberships.keys[pa.id], self.memberships.keys[pb.id]
                union_size = len(ka) + len(kb) - count
                sim = count / union_size if union_size else 0.0
                if sim >= threshold:
                    dupes.append(DupeFinding(pa, pb, ka, kb, count, sim))
                elif count:
                    overlaps.append(OverlapFinding(pa, pb, ka, kb, count))
            positions = {p.id: index for index, p in enumerate(self.playlists)}
            def tie(f):
                return positions[f.playlist_a.id], positions[f.playlist_b.id]
            self._partitions[threshold] = (
                sorted(dupes, key=lambda f: (-f.similarity, *tie(f))),
                sorted(overlaps, key=lambda f: (-f.shared_count, *tie(f))))
        return self._partitions[threshold]


def prepare_cleanup(store) -> CleanupAnalysis:
    """Read one coherent snapshot, then release persistence before analysis."""
    if getattr(store, '_read_only', False):
        context = nullcontext(store)
    else:
        context = store.read_snapshot()
    with context as reader:
        reader = getattr(reader, 'store', reader)
        memberships = reader.playlists.cleanup_memberships()
        preferences = reader.overlaps.cleanup_preferences()
    return CleanupAnalysis(memberships, preferences)


def _analysis(source) -> CleanupAnalysis:
    return source if isinstance(source, CleanupAnalysis) else prepare_cleanup(source)

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
    return merge_signature(p.scope_ref for p in playlists)


def find_identical_groups(store, ignored_sigs=None):
    """Cluster playlists by identical track set so N copies show as one group, not N-choose-2 pairs.

    Empty playlists are excluded (they aren't really "the same playlist", see find_empty_playlists).
    `ignored_sigs`: merge signatures the user dismissed. Those groups are hidden.
    """
    ignored_sigs = ignored_sigs or set()
    state = _analysis(store)
    by_keys = {}
    for p in state.playlists:
        keys = state.memberships.keys[p.id]
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
    playlists: list      # connected cluster of near-duplicate relationships (2+)
    avg_similarity: float

def find_near_duplicate_groups(store, threshold=0.70, exclude_playlist_ids=None, ignored_sigs=None):
    """Cluster near-duplicates into connected components for group-scoped merge dismissals.
    Identical playlists are handled by find_identical_groups and excluded here.

    `ignored_sigs`: merge signatures the user dismissed. Those groups are hidden."""
    exclude = exclude_playlist_ids or set()
    ignored_sigs = ignored_sigs or set()
    state = _analysis(store)
    edges = [d for d in find_dupes(state, threshold)
             if not d.identical
             and d.playlist_a.id not in exclude and d.playlist_b.id not in exclude]
    adj = {}
    for d in edges:
        a, b = d.playlist_a.id, d.playlist_b.id
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    pls = {p.id: p for p in state.playlists}
    seen, groups, component = set(), [], {}
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
        component.update((pid, len(groups)) for pid in comp)
        groups.append(NearGroup(members, 0.0))
    totals, counts = [0.0] * len(groups), [0] * len(groups)
    for edge in edges:
        group = component.get(edge.playlist_a.id)
        if group is not None:
            totals[group] += edge.similarity
            counts[group] += 1
    for i, group in enumerate(groups):
        group.avg_similarity = totals[i] / counts[i]
    return sorted(groups, key=lambda g: len(g.playlists), reverse=True)

def find_empty_playlists(store, ignored=None):
    """User playlists with no tracks (excludes undeletable system playlists like Liked Music).
    `ignored`: ytm ids dismissed from the Empty category."""
    ignored = ignored or set()
    state = _analysis(store)
    return sorted((p for p in state.playlists if p.scope_ref not in ignored
                   and not state.memberships.keys[p.id]),
                  key=lambda p: p.title.lower())

def find_tiny_playlists(store, max_tracks=3, ignored=None):
    """Manageable playlists with 1..max_tracks tracks, candidates to merge away or prune.
    `ignored`: ytm ids dismissed from the Tiny category."""
    ignored = ignored or set()
    state = _analysis(store)
    out = []
    for p in state.playlists:
        if p.scope_ref in ignored:
            continue
        n = len(state.memberships.keys[p.id])
        if 1 <= n <= max_tracks:
            out.append((n, p))
    return [p for _, p in sorted(out, key=lambda np: (np[0], np[1].title.lower()))]

def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)

def find_dupes(store, threshold=0.70):
    return _analysis(store).relationships(threshold)[0]

def _overlap_visible(pa, pb, suppressed, ignored_ytm, kept):
    pair = frozenset((pa.scope_ref, pb.scope_ref))
    if suppressed and pair in suppressed:
        return False
    if kept and pair in kept:
        return True
    return not ignored_ytm or (pa.scope_ref not in ignored_ytm
                               and pb.scope_ref not in ignored_ytm)

def near_duplicate_pairs(store, groups, suppressed=None, ignored_ytm=None, kept=None,
                         threshold=0.70):
    """Show each actual near-duplicate relationship with the same preferences as other overlaps.

    Groups have already excluded exact duplicates and previously dismissed merge suggestions.
    A connected group can contain a less-similar pair, so only emit pairs meeting the threshold.
    """
    state = _analysis(store)
    membership = {p.id: i for i, group in enumerate(groups) for p in group.playlists}
    ordered = [[] for _ in groups]
    for d in find_dupes(state, threshold):
        pa, pb = d.playlist_a, d.playlist_b
        group = membership.get(pa.id)
        if (group is not None and membership.get(pb.id) == group and d.similarity < 1
                and _overlap_visible(pa, pb, suppressed, ignored_ytm, kept)):
            # Groups sort members by id. Preserve their original pair/tie order.
            if pa.id > pb.id:
                pa, pb = pb, pa
            ordered[group].append(OverlapFinding(pa, pb, state.memberships.keys[pa.id],
                state.memberships.keys[pb.id], d.shared_count))
    out = [p for bucket in ordered for p in sorted(bucket, key=lambda p: (p.playlist_a.id, p.playlist_b.id))]
    return sorted(out, key=lambda f: f.shared_count, reverse=True)

def find_overlaps(store, dupe_threshold=0.70, exclude_playlist_ids=None, suppressed=None,
                  ignored_ytm=None, kept=None):
    # exclude_playlist_ids: playlists already shown as duplicates. Hide them here so the same
    # playlist doesn't appear in both sections (resolve dupes first; the rest resurface on re-sync).
    # suppressed: set of frozenset({ytm_a, ytm_b}) pairs the user has manually hidden.
    # ignored_ytm: playlists the user excludes from overlap detection entirely (e.g. a huge mixtape).
    # kept: pairs to ALWAYS keep visible even if one side is ignored (the "keep this pair" exception).
    exclude = exclude_playlist_ids or set()
    out = []
    for finding in _analysis(store).relationships(dupe_threshold)[1]:
        pa, pb = finding.playlist_a, finding.playlist_b
        if pa.id in exclude or pb.id in exclude:
            continue
        if not _overlap_visible(pa, pb, suppressed, ignored_ytm, kept):
            continue
        out.append(finding)
    return out

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
        """The tiny, JSON-serializable shape the home card needs, cached as a rec proposal so the
        home page never re-runs relationship analysis on every load. See rec.actions.refresh_cleanup."""
        return {"count": self.count, "thumbnails": self.thumbnails(2)}

@dataclass
class CleanupResult:
    analysis: CleanupAnalysis
    groups: list[IdenticalGroup]
    near_pairs: list[OverlapFinding]
    overlaps: list[OverlapFinding]
    empties: list[Playlist]
    tiny: list[Playlist]

    def summary(self) -> CleanupSummary:
        involved = {}
        for g in self.groups:
            for p in g.playlists:
                involved.setdefault(p.id, p)
        for o in self.near_pairs:
            involved.setdefault(o.playlist_a.id, o.playlist_a)
            involved.setdefault(o.playlist_b.id, o.playlist_b)
        for p in (*self.empties, *self.tiny):
            involved.setdefault(p.id, p)
        for o in self.overlaps:
            involved.setdefault(o.playlist_a.id, o.playlist_a)
            involved.setdefault(o.playlist_b.id, o.playlist_b)
        return CleanupSummary(list(involved.values()))


def analyze_cleanup(store) -> CleanupResult:
    state = _analysis(store)
    prefs = state.preferences
    ci = prefs.ignored
    ignored_sigs = {m['signature'] for m in prefs.merges}
    groups = find_identical_groups(state, ignored_sigs=ignored_sigs)
    exact_ids = {p.id for g in groups for p in g.playlists}
    near_groups = find_near_duplicate_groups(state, exclude_playlist_ids=exact_ids,
                                             ignored_sigs=ignored_sigs)
    overlap_prefs = {'suppressed': {frozenset((a, b)) for a, b, _ in prefs.suppressed},
                     'ignored_ytm': prefs.ignored_ytm, 'kept': prefs.kept}
    near_pairs = near_duplicate_pairs(state, near_groups, **overlap_prefs)
    dupe_ids = {p.id for d in find_dupes(state) for p in (d.playlist_a, d.playlist_b)}
    overlaps = find_overlaps(state, exclude_playlist_ids=dupe_ids, **overlap_prefs)
    return CleanupResult(state, groups, near_pairs, overlaps,
                         find_empty_playlists(state, ignored=ci.get('empty')),
                         find_tiny_playlists(state, ignored=ci.get('tiny')))


def cleanup_summary(store):
    """Collapse everything the /cleanup page surfaces into one 'is there anything to tidy?' answer:
    the distinct playlists involved across all cleanup categories (exact + near duplicates, empties,
    tiny playlists, and overlaps), minus any the user has ignored. Mirrors the cleanup page's own
    exclusions so a fully-triaged library reports nothing. Insertion order is dupes-first, so the
    thumbnails it surfaces are the playlists most likely to have cover art."""
    return (store if isinstance(store, CleanupResult) else analyze_cleanup(store)).summary()

