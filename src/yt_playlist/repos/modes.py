"""ModesRepo: persistence for taste modes (issue #60, Part A).

One row per discovered taste mode. `mode_id` is stable across recomputes (matched by centroid cosine
in rec/taste_modes.reconcile). active=0 rows are retired modes kept for history, hidden from the
default listing. We store the content-space centroid (float32 blob) plus denormalized metadata
(label, family histogram, representative tracks); membership is derived on demand elsewhere, not
stored here. Also exposes the two small track lookups the modes feature needs (genre for labeling,
title/artist for the /taste view)."""
import json

import numpy as np

from yt_playlist.repos.base import Repo, synchronized


class ModesRepo(Repo):
    @synchronized
    def list_modes(self, active_only=True) -> list[dict]:
        sql = "SELECT * FROM taste_modes"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY size DESC, mode_id ASC"
        out = []
        for r in self.conn.execute(sql).fetchall():
            out.append({
                "mode_id": r["mode_id"],
                "label": r["label"],
                "families": json.loads(r["families"]),
                "centroid": np.frombuffer(r["centroid"], dtype=np.float32).copy(),
                "size": r["size"],
                "rep_keys": json.loads(r["rep_keys"]),
                "active": r["active"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
            })
        return out

    @synchronized
    def next_mode_id(self) -> int:
        row = self.conn.execute("SELECT MAX(mode_id) m FROM taste_modes").fetchone()
        return (row["m"] or 0) + 1

    @synchronized
    def replace_modes(self, upserts, retired_ids, now) -> None:
        now = float(now)
        for u in upserts:
            blob = np.asarray(u["centroid"], dtype=np.float32).tobytes()
            fam = json.dumps([list(f) for f in u["families"]])
            reps = json.dumps(list(u["rep_keys"]))
            existing = self.conn.execute(
                "SELECT first_seen FROM taste_modes WHERE mode_id=?", (u["mode_id"],)).fetchone()
            first_seen = existing["first_seen"] if existing is not None else now
            self.conn.execute(
                "INSERT OR REPLACE INTO taste_modes"
                "(mode_id, label, families, centroid, size, rep_keys, active, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (u["mode_id"], u["label"], fam, blob, int(u["size"]), reps, first_seen, now))
        for mid in retired_ids:
            self.conn.execute("UPDATE taste_modes SET active=0 WHERE mode_id=?", (mid,))
        self.conn.commit()

    @synchronized
    def genres_for(self, keys) -> dict:
        if not keys:
            return {}
        q = ",".join("?" * len(keys))
        rows = self.conn.execute(
            f"SELECT identity_key, MIN(genre) genre FROM tracks "
            f"WHERE identity_key IN ({q}) AND genre IS NOT NULL AND genre<>'' "
            f"GROUP BY identity_key", list(keys)).fetchall()
        return {r["identity_key"]: r["genre"] for r in rows}

    @synchronized
    def meta_for(self, keys) -> dict:
        if not keys:
            return {}
        q = ",".join("?" * len(keys))
        rows = self.conn.execute(
            f"SELECT identity_key, MIN(title) title, MIN(artist) artist FROM tracks "
            f"WHERE identity_key IN ({q}) GROUP BY identity_key", list(keys)).fetchall()
        return {r["identity_key"]: {"title": r["title"], "artist": r["artist"]} for r in rows}

    @synchronized
    def years_for(self, keys) -> dict:
        """{identity_key: release_year_int} for tracks with a usable 4-digit mb_year (#63 temporal card)."""
        if not keys:
            return {}
        q = ",".join("?" * len(keys))
        rows = self.conn.execute(
            f"SELECT identity_key, MIN(mb_year) y FROM tracks "
            f"WHERE identity_key IN ({q}) AND mb_year GLOB '[0-9][0-9][0-9][0-9]*' "
            f"GROUP BY identity_key", list(keys)).fetchall()
        return {r["identity_key"]: int(r["y"][:4]) for r in rows}

    @synchronized
    def log_impressions(self, epoch, rows, now) -> None:
        """Record which mode (and, from #57, which in-mode ranker) filled each card lane for this menu
        epoch. Idempotent per (epoch, lane). Each row is (lane, mode_id) or (lane, mode_id, ranker)."""
        for row in rows:
            lane, mode_id = row[0], row[1]
            ranker = row[2] if len(row) > 2 else None
            self.conn.execute(
                "INSERT OR IGNORE INTO rec_mode_impressions(epoch, lane, mode_id, ranker, created_at) "
                "VALUES (?, ?, ?, ?, ?)", (int(epoch), lane, int(mode_id), ranker, float(now)))
        self.conn.commit()

    @synchronized
    def log_pick(self, playlist_id, mode_id, now, ranker=None) -> None:
        """Record a Save & play of a mode card, with the in-mode ranker it was served under (#57).
        Idempotent per playlist_id."""
        self.conn.execute(
            "INSERT OR IGNORE INTO rec_mode_picks(playlist_id, mode_id, ranker, created_at) "
            "VALUES (?, ?, ?, ?)", (int(playlist_id), int(mode_id), ranker, float(now)))
        self.conn.commit()

    @synchronized
    def impression_counts(self, since=None) -> dict:
        sql = "SELECT mode_id, COUNT(*) c FROM rec_mode_impressions"
        args = []
        if since is not None:
            sql += " WHERE created_at >= ?"
            args.append(float(since))
        sql += " GROUP BY mode_id"
        return {r["mode_id"]: r["c"] for r in self.conn.execute(sql, args).fetchall()}

    @synchronized
    def pick_rows(self, since=None) -> list:
        sql = "SELECT playlist_id, mode_id FROM rec_mode_picks"
        args = []
        if since is not None:
            sql += " WHERE created_at >= ?"
            args.append(float(since))
        return [(r["playlist_id"], r["mode_id"]) for r in self.conn.execute(sql, args).fetchall()]

    @synchronized
    def ranker_impression_counts(self, since=None) -> dict:
        """#57 {ranker: impressions}; NULL (pre-#57 rows) counts as 'cosine'."""
        sql = "SELECT COALESCE(ranker, 'cosine') r, COUNT(*) c FROM rec_mode_impressions"
        args = []
        if since is not None:
            sql += " WHERE created_at >= ?"
            args.append(float(since))
        sql += " GROUP BY r"
        return {row["r"]: row["c"] for row in self.conn.execute(sql, args).fetchall()}

    @synchronized
    def ranker_pick_rows(self, since=None) -> list:
        """#57 [(playlist_id, ranker)]; NULL counts as 'cosine'."""
        sql = "SELECT playlist_id, COALESCE(ranker, 'cosine') r FROM rec_mode_picks"
        args = []
        if since is not None:
            sql += " WHERE created_at >= ?"
            args.append(float(since))
        return [(row["playlist_id"], row["r"]) for row in self.conn.execute(sql, args).fetchall()]
