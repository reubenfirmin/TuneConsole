"""Temporary genre backfill for fully Themed radio (#286).

TODO #141: replace this genre endpoint workaround with replenishing cluster-space
streams. Bound each catalog read without making a prepared card's pool the limit
of an ongoing session. This path never fits a model or loads a library matrix.
"""
from yt_playlist.rec.radio_genres import GenrePolicy
from yt_playlist.repos.base import LIKED_EXISTS


def themed_pool(store, genres: GenrePolicy, excluded, excluded_keys, allowed, *, limit):
    """Read another eligible genre batch, including aliases and upload overrides."""
    if not genres.pinned or not (genres.current or {}).get("genre"):
        return []
    with store._lock:
        labels = [r[0] for r in store.conn.execute(
            "SELECT DISTINCT genre FROM recordings WHERE genre IS NOT NULL "
            "UNION SELECT json_extract(value_json,'$') FROM entry_field_choices WHERE field='genre'")
            if isinstance(r[0], str) and genres.allows({"genre": r[0]})]
    if not labels:
        return []
    placeholders = ",".join("?" * len(labels))
    tracks, seen, after = [], set(excluded_keys), 0
    while len(tracks) < limit:
        with store._lock:
            rows = store.conn.execute(
                "SELECT t.id, t.identity_key AS key, t.video_id, t.title, t.artist, t.album, t.thumbnail, "
                "t.duration_s AS duration, t.genre, t.mb_year AS year, t.available, t.bpm, t.energy, t.danceability, "
                "COALESCE(p.plays,0) AS plays, "
                f"{LIKED_EXISTS} AS liked, "
                "NOT EXISTS(SELECT 1 FROM catalog_entries e JOIN library_memberships m ON m.entry_id=e.id "
                "WHERE e.recording_id=t.recording_id) AS _radio_fresh "
                "FROM catalog_tracks t LEFT JOIN listening_day_counts p ON p.identity_key=t.identity_key "
                f"WHERE t.id>? AND t.genre IN ({placeholders}) "
                "AND NULLIF(trim(t.video_id),'') IS NOT NULL AND COALESCE(t.available,1)<>0 "
                "ORDER BY t.id LIMIT ?", (after, *labels, limit)).fetchall()
        if not rows:
            break
        after = rows[-1]["id"]
        for row in rows:
            track = dict(row)
            if track["video_id"] in excluded or track["key"] in seen or not allowed(track):
                continue
            seen.add(track["key"])
            tracks.append(track)
            if len(tracks) == limit:
                break
    return tracks
