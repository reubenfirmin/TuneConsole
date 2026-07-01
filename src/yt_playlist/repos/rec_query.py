"""RecQueryRepo: read-only library queries and recommendation candidate generators.

These are the rec engine's reads over the library (tracks / playlist_tracks / history_items):
library primitives (keys, genres, distributions), the generated-playlist quarantine, and the
candidate-surface generators (comfort / rotation / deep cuts / completion / enrichment). They're
grouped here because the generators all depend on the same exclusion logic (excluded_playlist_ids).
"""
from collections import Counter

from yt_playlist.repos.base import GENERATED_GROUP, Repo, synchronized  # noqa: F401  (GENERATED_GROUP re-exported)
from yt_playlist.util import genre_map
from yt_playlist.util.matching import normalize, search_squash


def _join_names(names, total) -> str:
    """'Ritmo', 'Ritmo & Shpongle', 'Ritmo, Shpongle & 3 more', for a connection reason sentence."""
    if not names:
        return ""
    extra = total - len(names)
    if extra > 0:
        return ", ".join(names) + f" & {extra} more"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " & " + names[-1]


def _fam_label(fam) -> str:
    """Display a genre family; the map's singleton 'other:<genre>' fallback shows as just the genre."""
    return fam.split("other:", 1)[-1] if fam.startswith("other:") else fam


def _decade(year, *, as_str=False):
    """Floor a year-ish value to its decade as an int (1993 or '1993-05-01' -> 1990), or None when the
    first four characters aren't a year. Centralises the `int(y[:4]) // 10 * 10` idiom that the decade
    queries below all share. Pass as_str=True for the '1990' string form the distribution maps key on."""
    s = str(year or "")[:4]
    if not s.isdigit():
        return None
    d = int(s) // 10 * 10
    return str(d) if as_str else d


def _exclude_clause(column, ids, *, connective="AND") -> str:
    """Build a "' AND col NOT IN (1,2,3)'" SQL fragment from a set of ids, or '' when empty. The ids
    are app-internal integer playlist ids (from excluded_playlist_ids), never user input, so inlining
    them is safe; int() coercion makes that guarantee explicit and keeps it injection-proof. Replaces
    three hand-rolled copies of this string-building across the candidate generators."""
    if not ids:
        return ""
    return f" {connective} {column} NOT IN ({','.join(str(int(i)) for i in ids)})"


# Co-occurrence basket caps for the embedding model (see rec_baskets). A basket bigger than its cap is
# dropped: it links too many tracks to carry a real signal. Structural sources get generous caps; the
# content sources (whole genre family / decade) are capped tighter since they would otherwise produce
# huge, low-information baskets. Tracks longer than _MAX_TRACK_DURATION_S are DJ mixes/compilations,
# not songs, so they're excluded from every basket.
_MAX_TRACK_DURATION_S = 1200   # 20 min: above this it's a mix/live set/compilation, not a track
_ARTIST_BASKET_CAP = 50        # an artist's whole catalogue is a weaker co-occurrence signal than a playlist
_CONTENT_BASKET_CAP = 80       # one genre family / one decade: cap hard, these get large fast
# Catch-all playlists (more than this many tracks) are excluded from the embedding + taste: they link
# everything to everything and add weak, low-information co-occurrence pairs. (A #38 experiment tried a
# genre-coherence test to keep big GENRE playlists, but temporal_recall showed no gain and the project
# moved toward behavior-weighted taste instead, so the simple size rule stands.)
_CATCHALL_SIZE_FLOOR = 120


def _not_a_mix(col="duration_s") -> str:
    """SQL predicate keeping only real songs (duration <= the cap, or unknown), excluding DJ mixes /
    live sets / concerts. The candidate generators apply it so a 30-minute mix never lands in a
    generated playlist of short songs (#44); mirrors the embedding's own basket filter. `col` is the
    duration column, optionally table-qualified (e.g. 't.duration_s')."""
    return f"({col} IS NULL OR {col} <= {_MAX_TRACK_DURATION_S})"


class RecQueryRepo(Repo):
    # --- library primitives ---
    @synchronized
    def key_for_video(self, video_id):
        """identity_key for a video_id (track rows carry video_id; the model is keyed by identity_key)."""
        r = self.conn.execute(
            "SELECT identity_key FROM tracks WHERE video_id=? LIMIT 1", (video_id,)).fetchone()
        return r["identity_key"] if r else None

    @synchronized
    def track_genres(self, keys) -> dict:
        """{identity_key: genre} for the given keys that have a genre (for labelling clusters)."""
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        return {r["identity_key"]: r["genre"] for r in self.conn.execute(
            f"SELECT identity_key, genre FROM tracks WHERE identity_key IN ({qs}) AND genre<>''", keys)}

    @synchronized
    def track_decades(self, keys) -> dict:
        """{identity_key: '1990'} for keys whose mb_year is a 4-digit year (floored to its decade)."""
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        out = {}
        for r in self.conn.execute(
            f"SELECT identity_key k, MIN(mb_year) y FROM tracks "
            f"WHERE identity_key IN ({qs}) AND mb_year<>'' GROUP BY identity_key", keys):
            d = _decade(r["y"], as_str=True)
            if d is not None:
                out[r["k"]] = d
        return out

    @synchronized
    def track_last_played(self, keys) -> dict:
        """{identity_key: newest play timestamp} for the given keys with any play history.
        Keys never played are absent (caller treats absence as 'coldest')."""
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        rows = self.conn.execute(
            f"SELECT hi.identity_key k, MAX(hs.taken_at) last FROM history_items hi "
            f"JOIN history_snapshots hs ON hs.id=hi.snapshot_id "
            f"WHERE hi.identity_key IN ({qs}) GROUP BY hi.identity_key", keys).fetchall()
        return {r["k"]: r["last"] for r in rows}

    @synchronized
    def track_artists(self, keys) -> dict:
        """{identity_key: artist} for the given keys that have an artist."""
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        return {r["identity_key"]: r["artist"] for r in self.conn.execute(
            f"SELECT identity_key, MIN(artist) artist FROM tracks "
            f"WHERE identity_key IN ({qs}) AND artist<>'' GROUP BY identity_key", keys)}

    @synchronized
    def track_popularity(self, keys) -> dict:
        """{identity_key: popularity} (Deezer rank) for keys carrying a popularity value; absent = unknown.
        Powers the 'pop:' mainstream axis (#43). MAX over duplicate video rows picks the most-popular variant."""
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        return {r["identity_key"]: r["p"] for r in self.conn.execute(
            f"SELECT identity_key, MAX(popularity) p FROM tracks "
            f"WHERE identity_key IN ({qs}) AND popularity IS NOT NULL GROUP BY identity_key", keys)}

    @synchronized
    def era_play_distribution(self) -> dict:
        """{decade: Σ(1 + play_count)} over dated tracks, deduped per song (mirrors genre version)."""
        rows = self.conn.execute(
            "WITH tp AS (SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key), "
            "     songs AS (SELECT DISTINCT identity_key, mb_year FROM tracks WHERE mb_year<>'') "
            "SELECT s.mb_year y, SUM(1 + COALESCE(tp.c, 0)) w FROM songs s "
            "LEFT JOIN tp ON tp.identity_key = s.identity_key GROUP BY s.identity_key, s.mb_year").fetchall()
        out: dict = {}
        for r in rows:
            d = _decade(r["y"], as_str=True)
            if d is not None:
                out[d] = out.get(d, 0) + r["w"]
        return out

    @synchronized
    def tracks_total(self) -> int:
        """Total tracks in the library (taste-model coverage denominator)."""
        return self.conn.execute("SELECT COUNT(*) c FROM tracks").fetchone()["c"]

    @synchronized
    def owned_albums(self) -> set:
        """Lowercased album titles already in the library or saved, to filter outward discovery."""
        rows = self.conn.execute(
            "SELECT LOWER(album) a FROM tracks WHERE album<>'' "
            "UNION SELECT LOWER(title) FROM saved_albums").fetchall()
        return {r["a"] for r in rows if r["a"]}

    @synchronized
    def library_keys(self) -> set:
        """All track identity_keys in the library, to filter 'fresh' (unowned) discovery."""
        return {r["identity_key"] for r in self.conn.execute(
            "SELECT DISTINCT identity_key FROM tracks")}

    @synchronized
    def library_artists(self) -> set:
        """Normalized artist names already in the library, to exclude from new-artist discovery."""
        rows = self.conn.execute("SELECT DISTINCT artist FROM tracks WHERE artist<>''").fetchall()
        return {normalize(r["artist"]) for r in rows}

    @synchronized
    def tracks_by_artists(self, artists) -> list:
        """Owned track dicts (one per identity_key) whose normalized artist is in `artists` (a set of
        normalized names). Powers the #28 artist model's track-candidate expansion. The identity_key's
        artist segment is already normalized, so it is matched directly."""
        want = set(artists)
        if not want:
            return []
        out = []
        for r in self.conn.execute(
                "SELECT identity_key k, MIN(title) title, MIN(artist) artist, MIN(album) album, "
                "MIN(video_id) vid, MIN(thumbnail) thumb FROM tracks WHERE title<>'' GROUP BY identity_key"):
            if r["k"].rsplit("|", 1)[-1] in want:
                out.append({"key": r["k"], "title": r["title"], "artist": r["artist"],
                            "album": r["album"] or "", "video_id": r["vid"], "thumbnail": r["thumb"]})
        return out

    @synchronized
    def saved_album_ids(self) -> set:
        return {r["browse_id"] for r in self.conn.execute(
            "SELECT browse_id FROM saved_albums")}

    @synchronized
    def track_content(self) -> dict:
        """{identity_key: (genre, year4)} for tagged tracks, features for the content→embedding map."""
        rows = self.conn.execute(
            "SELECT identity_key k, MIN(genre) g, MIN(mb_year) y FROM tracks "
            "WHERE genre<>'' GROUP BY identity_key").fetchall()
        out = {}
        for r in rows:
            y = r["y"][:4] if (r["y"] and r["y"][:4].isdigit()) else None
            out[r["k"]] = (r["g"], y)
        return out

    # Audio features that feed the content vector's "sounds-like" block (see embed.build_content_vectors).
    AUDIO_COLS = ("bpm", "energy", "danceability", "mood_happy", "mood_sad", "mood_relaxed",
                  "mood_acoustic", "instrumental", "loudness", "dynamic_complexity",
                  "music_key", "music_scale")

    @synchronized
    def artist_genres(self) -> dict:
        """{artist: dominant genre} over the library, the most common non-empty genre per artist.
        Lets outward discovery tag a new ALBUM by its (owned) artist's known genre, no network."""
        rows = self.conn.execute(
            "SELECT artist, genre, COUNT(*) c FROM tracks WHERE artist<>'' AND genre<>'' "
            "GROUP BY artist, genre").fetchall()
        best = {}
        for r in rows:
            a = r["artist"]
            if a not in best or r["c"] > best[a][1]:
                best[a] = (r["genre"], r["c"])
        return {a: g for a, (g, _) in best.items()}

    @synchronized
    def track_audio_features(self) -> dict:
        """{identity_key: {feature: value}} for tracks carrying any audio metadata. Continuous
        AcousticBrainz/Deezer features plus the categorical music_key / music_scale. One row per
        identity_key (MIN over duplicate video rows); absent features are simply omitted from the
        per-key dict, so the content builder can z-score over only what's present."""
        cols = self.AUDIO_COLS
        sel = ", ".join(f"MIN({c}) {c}" for c in cols)
        where = " OR ".join(f"{c} IS NOT NULL" for c in cols)
        rows = self.conn.execute(
            f"SELECT identity_key k, {sel} FROM tracks WHERE {where} GROUP BY identity_key").fetchall()
        out = {}
        for r in rows:
            d = {c: r[c] for c in cols if r[c] is not None and r[c] != ""}
            if d:
                out[r["k"]] = d
        return out

    @synchronized
    def tracks_by_keys(self, keys) -> dict:
        """Display metadata for a set of identity_keys: {key: {title, artist, album, video_id, thumbnail}}."""
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        rows = self.conn.execute(
            f"SELECT identity_key k, MIN(title) title, MIN(artist) artist, MIN(album) album, "
            f"       MIN(video_id) vid, MIN(thumbnail) thumb FROM tracks "
            f"WHERE identity_key IN ({qs}) GROUP BY identity_key", keys).fetchall()
        return {r["k"]: {"title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                         "video_id": r["vid"], "thumbnail": r["thumb"]} for r in rows}

    @synchronized
    def connection_facts(self, key, path_keys, max_playlist=120, max_artists=3) -> list[dict]:
        """Why are two Clusters tracks linked? The grounded co-occurrence facts behind an edge.

        Reads the same baskets that fed the taste embedding (`rec_baskets`) and turns the concrete
        ones into human-readable reasons: same artist, shared playlists (naming the co-occurring
        path artists), shared album, same listening session, genre family, decade. `key` = the
        child track; `path_keys` = its PINNED path (central seeds + ancestors). Returned
        strongest-first as [{kind, text}, ...]. Empty when the link is purely second-order (no
        direct shared basket) and the caller falls back to an embedding 'bridge'
        (see embed.connection_geometry). Catch-all playlists (> max_playlist tracks) and quarantined
        generated playlists are excluded, mirroring the embedding's own basket filtering."""
        path = [k for k in dict.fromkeys(path_keys) if k and k != key]
        if not key or not path:
            return []
        qs = ",".join("?" * len(path))
        crow = self.conn.execute(
            "SELECT MIN(artist) artist, MIN(NULLIF(album,'')) album, MIN(NULLIF(genre,'')) genre, "
            "MIN(NULLIF(mb_year,'')) yr FROM tracks WHERE identity_key=?", (key,)).fetchone()
        if crow is None:
            return []
        c_artist = (crow["artist"] or "").strip()
        # Strongest-first; each phase returns one fact dict or None.
        facts = [
            self._fact_same_artist(key, path, qs),
            self._fact_playlists(key, path, qs, c_artist, max_playlist, max_artists),
            self._fact_album(key, path, qs),
            self._fact_session(key, path, qs),
            self._fact_genre(crow, path, qs),
            self._fact_decade(crow, path, qs),
        ]
        return [f for f in facts if f]

    # connection_facts phases (each returns one fact dict or None; called under its lock).
    def _fact_same_artist(self, key, path, qs):
        """Most certain link: the child shares an artist with something else on the branch."""
        row = self.conn.execute(
            f"SELECT t2.artist a FROM tracks t1 JOIN tracks t2 "
            f"ON LOWER(TRIM(t2.artist))=LOWER(TRIM(t1.artist)) "
            f"WHERE t1.identity_key=? AND TRIM(t1.artist)<>'' AND t2.identity_key IN ({qs}) LIMIT 1",
            [key, *path]).fetchone()
        return ({"kind": "same_artist", "text": f"Same artist ({row['a']}) as elsewhere on this branch."}
                if row else None)

    def _fact_playlists(self, key, path, qs, c_artist, max_playlist, max_artists):
        """Shared playlists: count distinct (excluding catch-alls > max_playlist and quarantined
        generated lists), naming up to max_artists of the co-occurring path artists."""
        excl = self.excluded_playlist_ids()
        rows = self.conn.execute(
            f"SELECT cp.pid pid, t.artist artist FROM "
            f"(SELECT DISTINCT pt.playlist_id pid FROM playlist_tracks pt "
            f" JOIN tracks t ON t.id=pt.track_id WHERE t.identity_key=?) cp "
            f"JOIN playlist_tracks pt ON pt.playlist_id=cp.pid "
            f"JOIN tracks t ON t.id=pt.track_id "
            f"WHERE t.identity_key IN ({qs}) "
            f"AND cp.pid NOT IN (SELECT playlist_id FROM playlist_tracks "
            f"                   GROUP BY playlist_id HAVING COUNT(*) > ?)",
            [key, *path, max_playlist]).fetchall()
        pids, artists = set(), []
        for r in rows:
            if r["pid"] in excl:
                continue
            pids.add(r["pid"])
            a = (r["artist"] or "").strip()
            if a and a.lower() != c_artist.lower() and a not in artists:
                artists.append(a)
        if not pids:
            return None
        n = len(pids)
        lead = f"In {n} of your playlist" + ("s" if n != 1 else "")
        names = _join_names(artists[:max_artists], len(artists))
        return {"kind": "playlist",
                "text": (f"{lead}, alongside {names}." if names else f"{lead} on this branch.")}

    def _fact_album(self, key, path, qs):
        """Shared album with another branch track."""
        row = self.conn.execute(
            f"SELECT t2.album al, t2.artist ar FROM tracks t1 JOIN tracks t2 "
            f"ON LOWER(t2.album)=LOWER(t1.album) "
            f"WHERE t1.identity_key=? AND TRIM(t1.album)<>'' AND t2.identity_key IN ({qs}) "
            f"AND t2.identity_key<>? LIMIT 1", [key, *path, key]).fetchone()
        return ({"kind": "album", "text": f"From the album “{row['al']}”, with {row['ar']}."}
                if row else None)

    def _fact_session(self, key, path, qs):
        """Played in the same listening session (a shared history snapshot)."""
        hit = self.conn.execute(
            f"SELECT 1 FROM history_items h1 JOIN history_items h2 ON h2.snapshot_id=h1.snapshot_id "
            f"WHERE h1.identity_key=? AND h2.identity_key IN ({qs}) LIMIT 1",
            [key, *path]).fetchone()
        return ({"kind": "session", "text": "You played them in the same listening session."}
                if hit else None)

    def _fact_genre(self, crow, path, qs):
        """Both sit in the same genre family (meta-genre map)."""
        if not crow["genre"]:
            return None
        cf = genre_map.family(crow["genre"])
        prows = self.conn.execute(
            f"SELECT DISTINCT genre FROM tracks WHERE identity_key IN ({qs}) AND genre<>''",
            path).fetchall()
        if cf and any(genre_map.family(r["genre"]) == cf for r in prows):
            return {"kind": "genre", "text": f"Both in your {_fam_label(cf)} family."}
        return None

    def _fact_decade(self, crow, path, qs):
        """Both from the same decade."""
        cd = _decade(crow["yr"])
        if cd is None:
            return None
        yrows = self.conn.execute(
            f"SELECT DISTINCT mb_year FROM tracks WHERE identity_key IN ({qs}) AND mb_year<>''",
            path).fetchall()
        if any(_decade(r["mb_year"]) == cd for r in yrows):
            return {"kind": "decade", "text": f"Both from the {cd}s."}
        return None

    @synchronized
    def library_genre_families(self) -> list[dict]:
        """Genre families present in the library, each with its track count, the option list for the
        Clusters genre-family filter (#29). Untagged tracks contribute nothing. Sorted most-common
        first, then alphabetically."""
        fams = {}
        for r in self.conn.execute(
                "SELECT genre, COUNT(DISTINCT identity_key) n FROM tracks "
                "WHERE genre IS NOT NULL AND genre<>'' GROUP BY genre"):
            fam = genre_map.family(r["genre"])
            fams[fam] = fams.get(fam, 0) + r["n"]
        return [{"family": f, "n": n} for f, n in
                sorted(fams.items(), key=lambda kv: (-kv[1], kv[0]))]

    @synchronized
    def keys_in_families(self, families) -> set:
        """Identity_keys whose genre maps into one of `families` (#29 whitelist). Untagged tracks are
        never included. A track with no genre can't be vouched into a restricted cluster."""
        fams = set(families)
        if not fams:
            return set()
        return {r["k"] for r in self.conn.execute(
            "SELECT DISTINCT identity_key k, genre FROM tracks WHERE genre IS NOT NULL AND genre<>''")
            if genre_map.family(r["genre"]) in fams}

    @synchronized
    def library_genres(self) -> list[dict]:
        """Individual genres (sub-genres) present in the library, each with its family and track count,
        the fine-grained options for the Clusters genre filter (#29), alongside the coarse families.
        Sorted most-common first, then alphabetically."""
        rows = self.conn.execute(
            "SELECT genre, COUNT(DISTINCT identity_key) n FROM tracks "
            "WHERE genre IS NOT NULL AND genre<>'' GROUP BY genre").fetchall()
        return [{"genre": r["genre"], "family": genre_map.family(r["genre"]), "n": r["n"]}
                for r in sorted(rows, key=lambda r: (-r["n"], r["genre"].lower()))]

    @synchronized
    def keys_in_genre_selection(self, tokens) -> set:
        """Identity_keys allowed by a Clusters genre whitelist (#29) where each token may be EITHER a
        genre family OR a specific genre (sub-genre). A track qualifies if its genre matches a token
        exactly, or its family matches a token. Untagged tracks are never included."""
        toks = {t.strip().lower() for t in tokens if t and t.strip()}
        if not toks:
            return set()
        out = set()
        for r in self.conn.execute(
                "SELECT DISTINCT identity_key k, genre FROM tracks WHERE genre IS NOT NULL AND genre<>''"):
            g = r["genre"].strip().lower()
            if g in toks or genre_map.family(r["genre"]).lower() in toks:
                out.add(r["k"])
        return out

    @synchronized
    def cluster_search(self, q, limit=8) -> list[dict]:
        """Autosuggest seeds for the Clusters canvas. Up to `limit` results per kind, each a dict
        {kind, label, sub, keys}: an artist (all their modelled tracks), a playlist (its modelled
        tracks, excluding the quarantined Generated group), or a single song. Only vector-backed
        identity_keys are returned. A seed with no vector adds nothing to a node's centroid.

        Matching is punctuation/space/accent-insensitive (searchnorm, #48): typing 'LSD' finds a
        track titled 'L.S.D.', and 'cafe' finds 'Café'. `qn` is the query reduced to that same key,
        so the LIKE patterns carry no special chars and need no ESCAPE."""
        qn = search_squash(q or "")
        if not qn:
            return []
        like = "%" + qn + "%"
        prefix = qn + "%"            # for relevance ranking: a title STARTING with the query beats a mid-string hit
        out = []
        for r in self.conn.execute(
                "SELECT t.artist label, COUNT(DISTINCT t.identity_key) n, "
                "       GROUP_CONCAT(DISTINCT t.identity_key) keys "
                "FROM tracks t JOIN rec_vectors rv ON rv.identity_key=t.identity_key "
                "WHERE searchnorm(t.artist) LIKE ? AND t.artist<>'' "
                "GROUP BY t.artist ORDER BY n DESC LIMIT ?", (like, limit)):
            out.append({"kind": "artist", "label": r["label"],
                        "sub": f"{r['n']} track" + ("" if r["n"] == 1 else "s"),
                        "keys": r["keys"].split(",")})
        for r in self.conn.execute(
                "SELECT p.title label, COUNT(DISTINCT t.identity_key) n, "
                "       GROUP_CONCAT(DISTINCT t.identity_key) keys "
                "FROM playlists p JOIN playlist_tracks pt ON pt.playlist_id=p.id "
                "JOIN tracks t ON t.id=pt.track_id "
                "JOIN rec_vectors rv ON rv.identity_key=t.identity_key "
                "LEFT JOIN playlist_group g ON g.ytm=p.ytm_playlist_id "
                "WHERE searchnorm(p.title) LIKE ? AND (g.name IS NULL OR g.name<>?) "
                "GROUP BY p.id ORDER BY n DESC LIMIT ?", (like, GENERATED_GROUP, limit)):
            out.append({"kind": "playlist", "label": r["label"],
                        "sub": f"{r['n']} track" + ("" if r["n"] == 1 else "s"),
                        "keys": r["keys"].split(",")})
        for r in self.conn.execute(
                "SELECT MIN(t.title) title, MIN(t.artist) artist, t.identity_key key "
                "FROM tracks t JOIN rec_vectors rv ON rv.identity_key=t.identity_key "
                "WHERE searchnorm(t.title) LIKE ? GROUP BY t.identity_key "
                # Relevance order so the best match lands within the limit: exact title first, then titles
                # starting with the query, then mid-string hits; ties broken by shorter title, then alpha.
                "ORDER BY CASE WHEN searchnorm(MIN(t.title)) = ? THEN 0 "
                "              WHEN searchnorm(MIN(t.title)) LIKE ? THEN 1 ELSE 2 END, "
                "         length(MIN(t.title)), MIN(t.title) LIMIT ?", (like, qn, prefix, limit)):
            out.append({"kind": "song", "label": r["title"], "sub": r["artist"],
                        "keys": [r["key"]]})
        return out

    @synchronized
    def top_played_keys(self, limit=10) -> list[str]:
        """Identity keys of your most-played songs (for seeding taste-neighbourhood recs)."""
        rows = self.conn.execute(
            "SELECT identity_key k, COUNT(*) c FROM history_items GROUP BY identity_key "
            "ORDER BY c DESC LIMIT ?", (limit,)).fetchall()
        return [r["k"] for r in rows]

    @synchronized
    def play_counts(self) -> dict:
        """{identity_key: total play count} from listening history. A key absent from the map has
        never been played (count 0), the primary signal for the Catalog card (your under-played
        catalog)."""
        return {r["identity_key"]: r["c"] for r in self.conn.execute(
            "SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key")}

    # --- genre distributions / adjacency ---
    @synchronized
    def genre_distribution(self) -> dict:
        """{genre: track_count} over tagged tracks, feeds the taste-breadth/palette computation."""
        return {r["genre"]: r["c"] for r in self.conn.execute(
            "SELECT genre, COUNT(*) c FROM tracks WHERE genre<>'' GROUP BY genre")}

    @synchronized
    def genre_play_distribution(self) -> dict:
        """{genre: Σ (1 + play_count)} over tagged tracks, play-weighted so a barely-played
        context counts toward breadth/palette far less than one you actually listen to (the +1 keeps
        owned-but-unplayed tracks from vanishing entirely)."""
        # Collapse to one row per (song, genre) FIRST: a song commonly has several `tracks` rows
        # (same identity_key, different video_id uploads). Without the DISTINCT, the LEFT JOIN below
        # would add (1 + plays) once per upload, multiplying a song's weight by its duplicate count.
        rows = self.conn.execute(
            "WITH tp AS (SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key), "
            "     songs AS (SELECT DISTINCT identity_key, genre FROM tracks WHERE genre <> '') "
            "SELECT s.genre g, SUM(1 + COALESCE(tp.c, 0)) w FROM songs s "
            "LEFT JOIN tp ON tp.identity_key = s.identity_key GROUP BY s.genre").fetchall()
        return {r["g"]: r["w"] for r in rows}

    @synchronized
    def genre_cooccurrence(self) -> dict:
        """How often each unordered genre pair shares a playlist: the corpus adjacency signal.

        Returns {"pairs": {(g1,g2): count}, "occ": {genre: #playlists}}. Used to pull genres the
        user repeatedly playlists together closer than the static map alone (spec §2.1/§5.3).
        """
        excl = self.excluded_playlist_ids()
        pl = {}
        for r in self.conn.execute(
            "SELECT pt.playlist_id pid, t.genre g FROM playlist_tracks pt "
            "JOIN tracks t ON t.id=pt.track_id WHERE t.genre<>''"):
            if r["pid"] in excl:                         # generated playlists don't shape adjacency
                continue
            pl.setdefault(r["pid"], set()).add(r["g"])
        pairs, occ = Counter(), Counter()
        for genres in pl.values():
            gs = sorted(genres)
            for g in gs:
                occ[g] += 1
            for i in range(len(gs)):
                for j in range(i + 1, len(gs)):
                    pairs[(gs[i], gs[j])] += 1
        return {"pairs": dict(pairs), "occ": dict(occ)}

    @synchronized
    def playlist_track_genres(self, playlist_id) -> list[str]:
        """Non-empty genres of a playlist's tracks (for the genre-diversity stat)."""
        return [r["g"] for r in self.conn.execute(
            "SELECT t.genre g FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=? AND t.genre<>''", (playlist_id,))]

    # --- generated-playlist quarantine (so the engine never feeds on its own suggestions) ---
    @synchronized
    def excluded_playlist_ids(self, group=GENERATED_GROUP) -> set:
        """DB ids of generated playlists still quarantined from every taste signal. A generated
        playlist (group == `group`) is excluded until you promote it. Move it out of the group so
        it counts as one of your real playlists. Playing it does NOT graduate it; only the explicit
        move into your library does (see /playlist/{id}/promote)."""
        rows = self.conn.execute(
            "SELECT p.id id FROM playlists p "
            "JOIN playlist_group g ON g.ytm=p.ytm_playlist_id WHERE g.name=:grp",
            {"grp": group}).fetchall()
        return {r["id"] for r in rows}

    @synchronized
    def generated_track_keys(self, group=GENERATED_GROUP) -> set:
        """Identity_keys of every track already sitting in a generated-group playlist, so the
        recommendation lanes never re-offer songs you've just bundled into one (you saved it; don't
        suggest it back). Independent of graduation: once it's in a generated playlist, it's spoken for."""
        return {r["identity_key"] for r in self.conn.execute(
            "SELECT DISTINCT t.identity_key FROM playlist_tracks pt "
            "JOIN tracks t ON t.id=pt.track_id JOIN playlists p ON p.id=pt.playlist_id "
            "JOIN playlist_group g ON g.ytm=p.ytm_playlist_id WHERE g.name=?", (group,))}

    @synchronized
    def generated_only_keys(self, group=GENERATED_GROUP) -> set:
        """Track keys that live ONLY in quarantined generated playlists, mirrors excluded_playlist_ids
        at the track level, so a generated song pollutes no embedding basket (album/artist/genre/year/
        session) until its playlist is promoted. Once the playlist is promoted, or the track also lands
        in a real playlist, it counts again. Plays don't lift the quarantine. Promotion does."""
        excl = self.excluded_playlist_ids(group)
        if not excl:
            return set()
        qs = ",".join("?" * len(excl))
        rows = self.conn.execute(
            "SELECT t.identity_key k FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "GROUP BY t.identity_key "
            f"HAVING SUM(CASE WHEN pt.playlist_id IN ({qs}) THEN 0 ELSE 1 END)=0",
            list(excl)).fetchall()
        return {r["k"] for r in rows}

    @synchronized
    @synchronized
    def catchall_playlist_ids(self, size_floor=_CATCHALL_SIZE_FLOOR) -> set:
        """Playlist ids too large to be a coherent listening context (more than `size_floor` tracks);
        excluded from the embedding and the taste centroids as low-information grab-bags."""
        return {r["pid"] for r in self.conn.execute(
            "SELECT playlist_id pid, COUNT(*) n FROM playlist_tracks GROUP BY playlist_id HAVING n > ?",
            (size_floor,))}

    def rec_baskets(self, max_album=30, max_session=120) -> list[list[str]]:
        """Co-occurrence baskets for the embedding model: playlists, albums, listening sessions.

        Catch-all playlists are excluded, but #38 makes that test SMART: a large playlist is dropped
        only when it is also genre-incoherent (a grab-bag). A focused genre playlist (one dominant
        family) is kept at any size, so big coherent collections feed the model. Live sets, full-
        performance uploads (UGC), and over-long "tracks" that are really DJ mixes/compilations are
        dropped too, since they co-occur with unrelated songs and blur the model. Each basket is a
        list of track identity_keys.
        """
        good = {r["k"] for r in self.conn.execute(
            "SELECT DISTINCT identity_key k FROM tracks "
            "WHERE (video_type IS NULL OR video_type <> 'MUSIC_VIDEO_TYPE_UGC') "
            "AND (duration_s IS NULL OR duration_s <= ?)", (_MAX_TRACK_DURATION_S,))}
        good -= self.generated_only_keys()               # quarantine generated songs until promoted
        excl = self.excluded_playlist_ids() | self.catchall_playlist_ids()   # generated + grab-bags
        pl_where = _exclude_clause("pt.playlist_id", excl, connective="WHERE")
        out = []
        # playlist baskets: the exclusion set already drops generated + catch-alls, so coherent
        # playlists are kept at ANY size (no per-basket size cap here).
        pbuckets: dict = {}
        for r in self.conn.execute(
                "SELECT pt.playlist_id g, t.identity_key k FROM playlist_tracks pt "
                "JOIN tracks t ON t.id=pt.track_id" + pl_where):
            if r["k"] in good:
                pbuckets.setdefault(r["g"], set()).add(r["k"])
        out += [list(s) for s in pbuckets.values() if len(s) > 1]
        # album / artist / session baskets keep their size caps (an over-cap album/session is noise).
        for grp, cap in (
            ("SELECT album g, identity_key k FROM tracks WHERE album<>''", max_album),
            ("SELECT artist g, identity_key k FROM tracks WHERE artist<>''", _ARTIST_BASKET_CAP),
            ("SELECT snapshot_id g, identity_key k FROM history_items", max_session)):
            buckets = {}
            for r in self.conn.execute(grp):
                if r["k"] in good:
                    buckets.setdefault(r["g"], set()).add(r["k"])
            out += [list(s) for s in buckets.values() if 1 < len(s) <= cap]
        # content baskets: genre FAMILY (meta-genre map §2.1) and year decade
        fam, yr = {}, {}
        for r in self.conn.execute("SELECT genre, mb_year, identity_key k FROM tracks "
                                   "WHERE genre<>'' OR mb_year<>''"):
            if r["k"] not in good:
                continue
            if r["genre"]:
                fam.setdefault(genre_map.family(r["genre"]), set()).add(r["k"])
            dec = _decade(r["mb_year"])
            if dec is not None:
                yr.setdefault(dec, set()).add(r["k"])
        out += [list(s) for s in fam.values() if 1 < len(s) <= _CONTENT_BASKET_CAP]
        out += [list(s) for s in yr.values() if 1 < len(s) <= _CONTENT_BASKET_CAP]
        return out

    # --- candidate-surface generators ---
    @synchronized
    def comfort_candidates(self, now, min_plays=4, recency_full_days=30, limit=50) -> list[dict]:
        """'Comfort listening': your high-rotation favorites, demoted the more recently you've heard
        them, reliable tracks you haven't reached for lately.

        play count = appearances across history snapshots; last_played = newest snapshot containing
        the song. Each track scores plays * min(1, days_since_last / recency_full_days): heavy
        rotation pushes it up, a recent play pulls it down (a track played today scores ~0). Only
        tracks with >= min_plays qualify, so this is always grounded in real listening. Never-played
        library tracks don't surface here (that's resurface/explore territory).
        """
        full_secs = max(1.0, recency_full_days * 86400.0)
        rows = self.conn.execute(
            "WITH plays AS (SELECT hi.identity_key, COUNT(*) c, MAX(hs.taken_at) last "
            "  FROM history_items hi JOIN history_snapshots hs ON hs.id=hi.snapshot_id "
            "  GROUP BY hi.identity_key), "
            "     names AS (SELECT identity_key, MIN(title) title, MIN(artist) artist, "
            "               MIN(album) album, MIN(video_id) vid, MIN(thumbnail) thumb "
            f"              FROM tracks WHERE {_not_a_mix()} GROUP BY identity_key) "
            "SELECT n.identity_key k, n.title, n.artist, n.album, n.vid, n.thumb, "
            "       p.c plays, p.last last "
            "FROM names n JOIN plays p ON p.identity_key=n.identity_key "
            "WHERE n.title <> '' AND p.c >= :min_plays "
            "ORDER BY p.c * min(1.0, (:now - p.last) / :full_secs) DESC, p.last ASC LIMIT :limit",
            {"min_plays": min_plays, "now": now, "full_secs": full_secs, "limit": limit}).fetchall()
        return [{"key": r["k"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "video_id": r["vid"], "thumbnail": r["thumb"], "plays": r["plays"],
                 "last_played": r["last"]} for r in rows]

    @synchronized
    def more_like_rotation(self, seed_limit=40, limit=40) -> list[dict]:
        """Tracks that share a playlist with your most-played songs but that you barely play.

        Collaborative signal: 'because you listen to X, and these live alongside X in your
        playlists.' Seeds = your top-played songs; candidates = co-members of their playlists.
        """
        excl = self.excluded_playlist_ids()
        seed_excl = _exclude_clause("pt.playlist_id", excl)
        rows = self.conn.execute(
            "WITH tp AS (SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key), "
            " seeds AS (SELECT identity_key k FROM tp ORDER BY c DESC LIMIT :seed_limit), "
            " seedpl AS (SELECT DISTINCT pt.playlist_id pid FROM playlist_tracks pt "
            "            JOIN tracks t ON t.id=pt.track_id JOIN seeds s ON s.k=t.identity_key"
            f"            WHERE 1=1{seed_excl}), "
            " cand AS (SELECT t.identity_key key, MIN(t.title) title, MIN(t.artist) artist, "
            "                 MIN(t.album) album, MIN(t.video_id) vid, MIN(t.thumbnail) thumb, "
            "                 COUNT(DISTINCT pt.playlist_id) sp, COALESCE(MAX(tp.c),0) plays "
            "          FROM playlist_tracks pt JOIN seedpl ON seedpl.pid=pt.playlist_id "
            "          JOIN tracks t ON t.id=pt.track_id "
            "          LEFT JOIN tp ON tp.identity_key=t.identity_key "
            f"          WHERE t.title<>'' AND {_not_a_mix('t.duration_s')} GROUP BY t.identity_key) "
            "SELECT key, title, artist, album, vid, thumb, sp, plays FROM cand "
            "WHERE key NOT IN (SELECT k FROM seeds) AND plays<=1 "
            "ORDER BY sp DESC, plays ASC, key LIMIT :limit",
            {"seed_limit": seed_limit, "limit": limit}).fetchall()
        return [{"key": r["key"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "video_id": r["vid"], "thumbnail": r["thumb"], "plays": r["plays"],
                 "shared_playlists": r["sp"]} for r in rows]

    @synchronized
    def deep_cuts(self, limit=40) -> list[dict]:
        """The least-played track of each artist you play a lot, 'you love them, revisit this.'

        Content/affinity signal that needs no history depth: ranks artists by total plays,
        surfaces each one's most-neglected track. Works on day one.
        """
        rows = self.conn.execute(
            "WITH tp AS (SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key), "
            " trk AS (SELECT t.identity_key key, MIN(t.title) title, MIN(t.artist) artist, "
            "                MIN(t.album) album, MIN(t.video_id) vid, MIN(t.thumbnail) thumb, "
            "                COALESCE(MAX(tp.c),0) plays "
            "         FROM tracks t LEFT JOIN tp ON tp.identity_key=t.identity_key "
            f"        WHERE t.title<>'' AND t.artist<>'' AND {_not_a_mix('t.duration_s')} "
            "         GROUP BY t.identity_key), "
            " ap AS (SELECT artist, SUM(plays) total FROM trk GROUP BY artist), "
            " r AS (SELECT trk.*, ap.total atot, "
            "        ROW_NUMBER() OVER (PARTITION BY trk.artist ORDER BY trk.plays ASC, trk.key) rn "
            "       FROM trk JOIN ap ON ap.artist=trk.artist WHERE ap.total>0) "
            "SELECT key, title, artist, album, vid, thumb, plays, atot FROM r WHERE rn=1 "
            "ORDER BY atot DESC, plays ASC, key LIMIT :limit",
            {"limit": limit}).fetchall()
        return [{"key": r["key"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "video_id": r["vid"], "thumbnail": r["thumb"], "plays": r["plays"],
                 "artist_plays": r["atot"]} for r in rows]

    @synchronized
    def complete_playlist(self, playlist_id, limit=20) -> list[dict]:
        """Tracks you own that fit a playlist but aren't in it.

        Fit = by an artist already in the playlist, and/or co-occurring with the playlist's
        tracks in your other playlists. Score weights same-artist above co-occurrence.
        """
        rows = self.conn.execute(
            "WITH pm AS (SELECT t.identity_key key, t.artist FROM playlist_tracks pt "
            "            JOIN tracks t ON t.id=pt.track_id WHERE pt.playlist_id=:pid), "
            " pa AS (SELECT DISTINCT artist FROM pm WHERE artist<>''), "
            " shared AS (SELECT DISTINCT pt.playlist_id pid FROM playlist_tracks pt "
            "            JOIN tracks t ON t.id=pt.track_id JOIN pm ON pm.key=t.identity_key "
            "            WHERE pt.playlist_id<>:pid), "
            " cand AS (SELECT t.identity_key key, MIN(t.title) title, MIN(t.artist) artist, "
            "                 MIN(t.album) album, MIN(t.video_id) vid, MIN(t.thumbnail) thumb, "
            "                 MAX(CASE WHEN t.artist IN (SELECT artist FROM pa) THEN 1 ELSE 0 END) sa, "
            "                 COUNT(DISTINCT CASE WHEN pt.playlist_id IN (SELECT pid FROM shared) "
            "                                THEN pt.playlist_id END) cooc "
            "          FROM tracks t JOIN playlist_tracks pt ON pt.track_id=t.id "
            f"          WHERE t.identity_key NOT IN (SELECT key FROM pm) AND t.title<>'' "
            f"          AND {_not_a_mix('t.duration_s')} "
            "          GROUP BY t.identity_key) "
            "SELECT key, title, artist, album, vid, thumb, sa, cooc FROM cand "
            "WHERE sa=1 OR cooc>0 ORDER BY (sa*2+cooc) DESC, cooc DESC, key LIMIT :limit",
            {"pid": playlist_id, "limit": limit}).fetchall()
        return [{"key": r["key"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "video_id": r["vid"], "thumbnail": r["thumb"],
                 "same_artist": bool(r["sa"]), "cooc": r["cooc"]} for r in rows]

    # enrichment_candidates / album_enrichment_candidates were removed along with the Take-Action
    # manual-enrichment nag cards they fed. The auto-enrich worker now handles coverage in the
    # background, so there is no surface that needs to rank "gappy" playlists/albums to nag about.
