"""RecModelRepo: the learned taste model: blend weights, feedback events, and embedding vectors.

Owns the rec_weights / rec_feedback / rec_vectors tables (created in store.py's central SCHEMA).
Split out of the former monolithic RecRepo so each rec concern is its own focused DAO.
"""
import time

from yt_playlist.repos.base import Repo, synchronized

# Learned blend weights live in [_WEIGHT_MIN, _WEIGHT_MAX] around the 1.0 prior: the floor stops a run
# of negative feedback from disabling an axis outright (a 0 weight would never win the for_you fair
# queue), the ceiling stops one axis from swamping the blend. Weights mean-revert toward 1.0 over time
# (see _reverted below) rather than shrinking a flat amount on every nudge, so stale learning decays
# but reinforced preferences hold.
_WEIGHT_MIN, _WEIGHT_MAX = 0.2, 3.0


def _reverted(weight, updated_at, now, halflife_d):
    """#85 time-proportional mean reversion toward the 1.0 prior: the gap halves every halflife_d
    days of NO reinforcement. A NULL updated_at (legacy row) reverts nothing until its next nudge
    stamps it. Replaces the old flat 5%-per-nudge shrink, which eroded stable preferences at the
    same rate as noise."""
    if updated_at is None or now is None:
        return weight
    age_d = max(0.0, (now - updated_at) / 86400.0)
    keep = 0.5 ** (age_d / float(halflife_d))
    return 1.0 + (weight - 1.0) * keep


class RecModelRepo(Repo):
    # --- learned blend weights ---
    @synchronized
    def get_weights(self, now=None, revert_halflife_d=60.0) -> dict:
        """Learned blend weights by axis (missing axis = prior 1.0), with time-proportional reversion
        toward 1.0 applied at read time (non-persisting: the stored value is untouched)."""
        now = time.time() if now is None else now
        return {r["axis"]: _reverted(r["weight"], r["updated_at"], now, revert_halflife_d)
                for r in self.conn.execute("SELECT axis, weight, updated_at FROM rec_weights")}

    @synchronized
    def nudge_weight(self, axis, factor, lo=_WEIGHT_MIN, hi=_WEIGHT_MAX, now=None, revert_halflife_d=60.0) -> float:
        """Revert the stored weight for elapsed time since its last nudge, then multiply by factor
        (clamped), then persist the result with updated_at=now."""
        now = time.time() if now is None else now
        row = self.conn.execute("SELECT weight, updated_at FROM rec_weights WHERE axis=?", (axis,)).fetchone()
        base = _reverted(row["weight"], row["updated_at"], now, revert_halflife_d) if row else 1.0
        w = max(lo, min(hi, base * factor))
        self.conn.execute("INSERT INTO rec_weights(axis, weight, updated_at) VALUES (?, ?, ?) "
                          "ON CONFLICT(axis) DO UPDATE SET weight=excluded.weight, updated_at=excluded.updated_at",
                          (axis, w, now))
        self.conn.commit()
        return w

    @synchronized
    def set_weight(self, axis, weight, lo=_WEIGHT_MIN, hi=_WEIGHT_MAX, now=None) -> None:
        """Manual override (Taste Model page). Clamped to the same [lo, hi] band as nudge_weight so a
        stray 0/negative can't silently disable a for_you lane (its fair-queue ratio would never win).
        A manual override is fresh evidence; it re-stamps the reversion clock."""
        now = time.time() if now is None else now
        w = max(lo, min(hi, float(weight)))
        self.conn.execute("INSERT INTO rec_weights(axis, weight, updated_at) VALUES (?, ?, ?) "
                          "ON CONFLICT(axis) DO UPDATE SET weight=excluded.weight, updated_at=excluded.updated_at",
                          (axis, w, now))
        self.conn.commit()

    @synchronized
    def reset_weights(self) -> None:
        self.conn.execute("DELETE FROM rec_weights")
        self.conn.commit()

    # --- feedback events (dismiss / less / more / mute / not_now) ---
    @synchronized
    def record_feedback(self, surface, item_key, kind, reason=None, scope="", until=None, now=None) -> None:
        """Persist a feedback event (dismiss/less/more/mute/not_now). Upserts per (surface,item,scope)."""
        self.conn.execute(
            "INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(surface,item_key,scope) DO UPDATE SET "
            "kind=excluded.kind, reason=excluded.reason, until=excluded.until, created_at=excluded.created_at",
            (surface, item_key, kind, reason, scope or "", until, now))
        self.conn.commit()

    @synchronized
    def suppressed_keys(self, surface, now, scope="") -> set:
        """Keys to hide on a surface: dismissed/muted/snoozed (surface-scoped) PLUS YouTube dislikes
        (global, every surface). All honor any 'until' expiry."""
        rows = self.conn.execute(
            "SELECT item_key FROM rec_feedback WHERE "
            "( (surface=? AND (scope='' OR scope=?) AND kind IN ('dismiss','mute','not_now')) "
            "  OR kind='dislike' ) "
            "AND (until IS NULL OR until>?)",
            (surface, scope or "", now)).fetchall()
        return {r["item_key"] for r in rows}

    def _record_once(self, surface, item_key, kind, until, now) -> bool:
        """Insert a first-seen feedback row (scope=''), idempotently. Returns True iff newly created,
        so the caller feeds transient/graduation exactly once. Shared by record_dislike/record_like."""
        if self.conn.execute(
                "SELECT 1 FROM rec_feedback WHERE surface=? AND item_key=? AND scope='' AND kind=?",
                (surface, item_key, kind)).fetchone():
            return False
        self.conn.execute(
            "INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at) "
            "VALUES (?,?,?,NULL,'',?,?)", (surface, item_key, kind, until, now))
        self.conn.commit()
        return True

    # --- YouTube dislikes (captured during sync; global long suppression, idempotent) ---
    @synchronized
    def record_dislike(self, identity_key, until, now) -> bool:
        """Persist a thumbs-down as a long global suppression (surface='sync'). Returns True iff newly
        created, so the caller feeds transient/graduation exactly once. Idempotent on re-sync."""
        return self._record_once("sync", identity_key, "dislike", until, now)

    @synchronized
    def clear_dislike(self, identity_key) -> None:
        """Reconcile an un-disliked track: drop its dislike row (un-suppress)."""
        self.conn.execute("DELETE FROM rec_feedback WHERE item_key=? AND kind='dislike'", (identity_key,))
        self.conn.commit()

    @synchronized
    def disliked_identity_keys(self) -> set:
        return {r["item_key"] for r in self.conn.execute(
            "SELECT item_key FROM rec_feedback WHERE kind='dislike'")}

    @synchronized
    def list_dislikes(self) -> list:
        """Active dislike bans (item_key, until, created_at), newest first, for the Taste Model page."""
        return self.conn.execute(
            "SELECT item_key, until, created_at FROM rec_feedback WHERE kind='dislike' "
            "ORDER BY created_at DESC").fetchall()

    # --- Likes (captured during sync; positive transient signal + graduation, idempotent) ---
    @synchronized
    def record_like(self, identity_key, now) -> bool:
        """Persist a first-seen like (surface='like'). Returns True iff newly created, so the caller
        feeds transient/graduation exactly once. Idempotent on re-sync. Mirrors record_dislike."""
        return self._record_once("like", identity_key, "like", None, now)

    @synchronized
    def recent_liked_keys(self, limit=None) -> list:
        """Liked identity_keys, most-recent (created_at) first. Powers the transient like channel."""
        rows = self.conn.execute(
            "SELECT item_key FROM rec_feedback WHERE kind='like' ORDER BY created_at DESC").fetchall()
        keys = [r["item_key"] for r in rows]
        return keys[:limit] if limit else keys

    @synchronized
    def recent_liked_with_ts(self, limit=None) -> list:
        """#85 [(identity_key, created_at)] for likes, newest-first (the timestamped sibling of
        recent_liked_keys; wall-clock decay needs the event time, not just the order)."""
        rows = self.conn.execute(
            "SELECT item_key, created_at FROM rec_feedback WHERE kind='like' "
            "ORDER BY created_at DESC").fetchall()
        out = [(r["item_key"], float(r["created_at"])) for r in rows]
        return out[:limit] if limit else out

    @synchronized
    def disliked_with_ts(self) -> list:
        """#85 [(identity_key, created_at)] for dislikes, newest-first, so the transient negative
        lean can decay by age instead of pressing at full strength forever."""
        rows = self.conn.execute(
            "SELECT item_key, created_at FROM rec_feedback WHERE kind='dislike' "
            "ORDER BY created_at DESC").fetchall()
        return [(r["item_key"], float(r["created_at"])) for r in rows]

    @synchronized
    def clear_like(self, identity_key) -> None:
        """Reconcile an un-liked track: drop its like row."""
        self.conn.execute("DELETE FROM rec_feedback WHERE item_key=? AND kind='like'", (identity_key,))
        self.conn.commit()

    # --- Standing slider leans (rec_lean): non-decaying transient multipliers, centered at 1.0 ---
    @synchronized
    def set_lean(self, axis, value, now) -> None:
        """Upsert a standing lean (home-slider position). value is a multiplier; 1.0 = neutral."""
        self.conn.execute(
            "INSERT INTO rec_lean(axis, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(axis) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (axis, float(value), now))
        self.conn.commit()

    @synchronized
    def get_leans(self) -> dict:
        """All standing leans {axis: value}. Absence of an axis means neutral 1.0."""
        return {r["axis"]: r["value"] for r in self.conn.execute("SELECT axis, value FROM rec_lean")}

    @synchronized
    def get_lean(self, axis) -> float:
        row = self.conn.execute("SELECT value FROM rec_lean WHERE axis=?", (axis,)).fetchone()
        return row["value"] if row else 1.0

    @synchronized
    def lean_rows(self) -> list:
        return self.conn.execute(
            "SELECT axis, value, updated_at, last_graduated_day FROM rec_lean").fetchall()

    @synchronized
    def set_lean_graduated_day(self, axis, day) -> None:
        self.conn.execute("UPDATE rec_lean SET last_graduated_day=? WHERE axis=?", (day, axis))
        self.conn.commit()

    @synchronized
    def clear_lean(self, axis) -> None:
        self.conn.execute("DELETE FROM rec_lean WHERE axis=?", (axis,))
        self.conn.commit()

    @synchronized
    def clear_all_leans(self) -> None:
        """Wipe every standing lean (Home 'Reset to default', bars back to neutral). Does NOT touch
        permanent weights (those are the long-term taste model, edited on the Taste page)."""
        self.conn.execute("DELETE FROM rec_lean")
        self.conn.commit()

    # --- Home bar curation (home_hidden_facet): which steering bars to SHOW; not a taste signal ---
    @synchronized
    def hidden_facets(self) -> set:
        """Axes the user removed from the Home panel (display-only)."""
        return {r["axis"] for r in self.conn.execute("SELECT axis FROM home_hidden_facet")}

    @synchronized
    def hide_facet(self, axis) -> None:
        self.conn.execute("INSERT OR IGNORE INTO home_hidden_facet(axis) VALUES (?)", (axis,))
        self.conn.commit()

    @synchronized
    def unhide_facet(self, axis) -> None:
        self.conn.execute("DELETE FROM home_hidden_facet WHERE axis=?", (axis,))
        self.conn.commit()

    @synchronized
    def clear_hidden_facets(self) -> None:
        self.conn.execute("DELETE FROM home_hidden_facet")
        self.conn.commit()

    @synchronized
    def muted_artists(self) -> set:
        """Artist names the user has muted (stored as item_key 'artist:<name>')."""
        rows = self.conn.execute("SELECT item_key FROM rec_feedback WHERE kind='mute'").fetchall()
        return {r["item_key"][7:] for r in rows if r["item_key"].startswith("artist:")}

    @synchronized
    def feedback_summary(self) -> dict:
        """{kind: count} of stored feedback events, for the Taste Model page."""
        return {r["kind"]: r["c"] for r in self.conn.execute(
            "SELECT kind, COUNT(*) c FROM rec_feedback GROUP BY kind")}

    @synchronized
    def clear_feedback(self) -> None:
        self.conn.execute("DELETE FROM rec_feedback")
        self.conn.execute("DELETE FROM rec_impressions")
        self.conn.commit()

    # --- taste-embedding vectors ---
    @synchronized
    def replace_rec_vectors(self, rows) -> None:
        """Atomically replace all taste-embedding vectors. rows = iterable of (identity_key, bytes)."""
        self.conn.execute("DELETE FROM rec_vectors")
        self.conn.executemany("INSERT INTO rec_vectors(identity_key, vec) VALUES (?,?)", rows)
        self.conn.commit()

    @synchronized
    def get_rec_vectors(self) -> list[tuple]:
        return [(r["identity_key"], r["vec"])
                for r in self.conn.execute("SELECT identity_key, vec FROM rec_vectors")]

    @synchronized
    def rec_vectors_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM rec_vectors").fetchone()["c"]

    # --- content (genre/era) vectors: parallel space for the cluster blend ---
    @synchronized
    def replace_rec_content_vectors(self, rows) -> None:
        """Atomically replace all content vectors. rows = iterable of (identity_key, bytes)."""
        self.conn.execute("DELETE FROM rec_content_vectors")
        self.conn.executemany("INSERT INTO rec_content_vectors(identity_key, vec) VALUES (?,?)", rows)
        self.conn.commit()

    @synchronized
    def get_rec_content_vectors(self) -> list[tuple]:
        return [(r["identity_key"], r["vec"])
                for r in self.conn.execute("SELECT identity_key, vec FROM rec_content_vectors")]

    @synchronized
    def rec_content_vectors_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM rec_content_vectors").fetchone()["c"]

    @synchronized
    def replace_rec_discovered_content_vectors(self, rows) -> None:
        """Atomically replace out-of-corpus (discovered) content vectors. rows = (identity_key, bytes)."""
        self.conn.execute("DELETE FROM rec_discovered_content_vectors")
        self.conn.executemany(
            "INSERT INTO rec_discovered_content_vectors(identity_key, vec) VALUES (?,?)", rows)
        self.conn.commit()

    @synchronized
    def get_rec_discovered_content_vectors(self) -> list[tuple]:
        return [(r["identity_key"], r["vec"])
                for r in self.conn.execute("SELECT identity_key, vec FROM rec_discovered_content_vectors")]

    # --- #28 artist-relationship model vectors (collaborative + content), keyed by normalized artist ---
    @synchronized
    def replace_rec_artist_vectors(self, rows) -> None:
        """Atomically replace all collaborative artist vectors. rows = iterable of (artist, bytes)."""
        self.conn.execute("DELETE FROM rec_artist_vectors")
        self.conn.executemany("INSERT INTO rec_artist_vectors(artist, vec) VALUES (?,?)", rows)
        self.conn.commit()

    @synchronized
    def get_rec_artist_vectors(self) -> list[tuple]:
        return [(r["artist"], r["vec"])
                for r in self.conn.execute("SELECT artist, vec FROM rec_artist_vectors")]

    @synchronized
    def rec_artist_vectors_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM rec_artist_vectors").fetchone()["c"]

    @synchronized
    def replace_rec_artist_content_vectors(self, rows) -> None:
        """Atomically replace all artist content vectors. rows = iterable of (artist, bytes)."""
        self.conn.execute("DELETE FROM rec_artist_content_vectors")
        self.conn.executemany("INSERT INTO rec_artist_content_vectors(artist, vec) VALUES (?,?)", rows)
        self.conn.commit()

    @synchronized
    def get_rec_artist_content_vectors(self) -> list[tuple]:
        return [(r["artist"], r["vec"])
                for r in self.conn.execute("SELECT artist, vec FROM rec_artist_content_vectors")]
