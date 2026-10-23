"""Resolve selected artists' genre and era neighborhoods through candidate acquisition.

This owner sees recipe inputs, artist contexts and passenger snapshots, never a complete draft or
job. Repairs return their consequences so the draft service can update its own axes and work queue.
"""
import math
import statistics
from collections import Counter
from dataclasses import dataclass
from itertools import zip_longest

from yt_playlist.rec import road_trip_axes as axes, road_trip_candidates as candidates
from yt_playlist.rec.road_trip_mix import _axis_genre
from yt_playlist.util import genre_map


@dataclass
class ClusterRepair:
    pool: list[dict]
    removed_inputs: set[str]
    changed: bool


@dataclass
class ClusterBatch:
    pool: list[dict]
    fresh: list[dict]


@dataclass
class ArtistNeighborhoods:
    artists: list[str]
    genres: list[str]
    contexts: dict[str, dict]

    def remember(self, name, songs, related):
        songs = [c for c in songs if c["artist"].casefold() == name.casefold()]
        genres = Counter(c["genre"] for c in songs if c["genre"])
        years = [c["year"] for c in songs[:5] if c.get("year")]
        self.contexts[name] = {"genre": genres.most_common(1)[0][0] if genres else "",
                               "year": round(statistics.median(years)) if years else None,
                               "related": list(related)}

    def for_genre(self, genre):
        """A seed anchors its own specific genre; a shared family would conflate distinct sliders."""
        genre = axes.canonical_genre(genre)
        return {name: context for name, context in self.contexts.items()
                if name in self.artists and genre and axes.canonical_genre(context["genre"]) == genre}

    def genre_input(self, pool, genre):
        genres = {axes.canonical_genre(g): g for g in self.genres}
        genre = axes.canonical_genre(genre)
        if genre in genres:
            return genres[genre]
        for candidate in pool:
            origin = axes.canonical_genre(candidate.get("genre_input") or candidate.get("input"))
            if _axis_genre(candidate) == genre and origin in genres:
                return genres[origin]
        return None

    def attribute_input(self, name, kind, fresh, existing, related, anchor=None):
        """Attach seed provenance or filter a related input to its seed's genre and era."""
        if kind == "artist" and name in self.artists:
            self.remember(name, fresh + existing, related)
            for candidate in fresh:
                candidate.update(cluster_genre=candidate["genre"], anchors=[name])
        elif anchor and (context := self.contexts.get(anchor)):
            family = genre_map.family(context["genre"])
            fresh = [c for c in fresh if self.anchors(c, {anchor: context})
                     and (not c["genre"] or c["genre"] == context["genre"]
                          or (family and c["family"] == family))]
            for candidate in fresh:
                candidate.update(cluster_genre=context["genre"], anchors=[anchor])
        return fresh

    @staticmethod
    def anchors(candidate, contexts):
        """Seeds keep their whole catalogs; one-hop peers need dates within ten years of the seed."""
        artist, anchors = candidate["artist"].casefold(), []
        for name, context in contexts.items():
            if artist == name.casefold():
                anchors.append(name)
            elif artist in {a.casefold() for a in context["related"]}:
                year, seed_year = candidate.get("year"), context.get("year")
                if seed_year is None or (year and abs(year - seed_year) <= 10):
                    anchors.append(name)
        return anchors

    def repair(self, pool):
        """Replace legacy family-wide assignments; defer pruning until all seed contexts exist."""
        contexts = {name: c for name, c in self.contexts.items() if name in self.artists}
        if not contexts or any(n not in contexts or not contexts[n]["genre"] for n in self.artists):
            return ClusterRepair(pool, set(), False)
        explicit = {axes.canonical_genre(g) for g in self.genres}
        kept, removed, changed = [], set(), False
        for candidate in pool:
            origin = candidate.get("genre_input") or candidate.get("input")
            if candidate["source"] == "mine" or not origin:
                kept.append(candidate)
                continue
            direct = next((n for n in contexts if n.casefold() == candidate["artist"].casefold()), None)
            anchors = [direct] if direct else self.anchors(candidate, contexts)
            anchors = [n for n in anchors if not contexts[n]["genre"] or not candidate["genre"]
                       or genre_map.family(contexts[n]["genre"]) == candidate["family"]]
            if anchors:
                matching = [n for n in anchors if n in self.for_genre(candidate.get("cluster_genre"))]
                if not matching:
                    matching = [n for n in anchors if n in self.for_genre(candidate["genre"])]
                genre = axes.canonical_genre(contexts[(matching or anchors)[0]]["genre"]) or candidate["genre"]
                anchors = [n for n in anchors if axes.canonical_genre(contexts[n]["genre"]) == genre]
                changed |= candidate.get("cluster_genre") != genre or candidate.get("anchors") != anchors
                candidate.update(cluster_genre=genre, anchors=anchors)
            elif axes.canonical_genre(origin) in explicit and not self.for_genre(_axis_genre(candidate)):
                changed |= bool(candidate.get("cluster_genre") or candidate.get("anchors"))
                candidate.update(cluster_genre="", anchors=[])
            else:
                removed.add(origin)
                changed = True
                continue
            kept.append(candidate)
        return ClusterRepair(kept, removed, changed)

    def ensure_contexts(self, store, client):
        """Hydrate old drafts only in the acquisition worker, using the original seed pages."""
        for name in self.artists:
            if name not in self.contexts:
                batch = candidates.artist_context(client, name)
                self.remember(name, candidates.to_candidates(batch.rows, store), batch.related)

    def resolve(self, pool, store, client, genre, cap):
        """Return a bounded seed neighborhood; None allows an independent genre acquisition."""
        self.ensure_contexts(store, client)
        contexts = self.for_genre(genre)
        if not contexts:
            if (self.artists and all(self.contexts.get(n, {}).get("genre") for n in self.artists)
                    and not self.genre_input(pool, genre)):
                return ClusterBatch(pool, [])  # an obsolete derived slider cannot broaden discovery
            return None
        genre = axes.canonical_genre(genre)
        family = genre_map.family(genre)
        kept = []
        for candidate in pool:
            if candidate["source"] == "theirs" and axes.matches(candidate, "genre:" + genre):
                anchors = self.anchors(candidate, contexts)
                if not anchors:
                    continue
                candidate.update(cluster_genre=genre, anchors=anchors)
            kept.append(candidate)
        known = {c["video_id"] for c in kept}
        neighborhoods = [[name] + context["related"] for name, context in contexts.items()]
        names = list(dict.fromkeys(n for group in zip_longest(*neighborhoods) for n in group if n))
        per_artist = max(2, math.ceil(cap / max(1, len(names))))
        out = []
        for name in names[:candidates.GENRE_ARTISTS]:
            if name in self.artists and name not in contexts:
                continue
            rows = [r for r in candidates.artist_songs(client, name) if r["video_id"] not in known]
            accepted = 0
            for candidate in candidates.to_candidates(rows[:per_artist * 3], store):
                anchors = self.anchors(candidate, contexts)
                compatible = (not candidate["genre"] or candidate["genre"] == genre
                              or (family and candidate["family"] == family))
                if not anchors or not compatible:
                    continue
                candidate.update(cluster_genre=genre, anchors=anchors, input=genre)
                out.append(candidate)
                known.add(candidate["video_id"])
                accepted += 1
                if len(out) >= cap:
                    return ClusterBatch(kept, out)
                if accepted >= per_artist:
                    break
        return ClusterBatch(kept, out)
