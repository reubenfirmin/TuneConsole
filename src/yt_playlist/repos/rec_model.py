"""RecModelRepo: the learned taste model: blend weights, feedback events, and embedding vectors.

Owns rec_weights / rec_feedback / rec_vectors; numbered migrations create their tables.
Split out of the former monolithic RecRepo so each rec concern is its own focused DAO.
"""
import json
import time
import uuid
from dataclasses import dataclass

from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.core import artists, catalog
from yt_playlist.util import genre_map

# Learned blend weights live in [_WEIGHT_MIN, _WEIGHT_MAX] around the 1.0 prior: the floor stops a run
# of negative feedback from disabling an axis outright (a 0 weight would never win the for_you fair
# queue), the ceiling stops one axis from swamping the blend. Weights mean-revert toward 1.0 over time
# (see _reverted below) rather than shrinking a flat amount on every nudge, so stale learning decays
# but reinforced preferences hold.
_WEIGHT_MIN, _WEIGHT_MAX = 0.2, 3.0

_VECTOR_TABLES = {"rec_vectors": "identity_key", "rec_content_vectors": "identity_key",
                  "rec_discovered_content_vectors": "identity_key", "rec_artist_vectors": "artist",
                  "rec_artist_content_vectors": "artist"}


@dataclass(frozen=True)
class VectorChanges:
    upserts: tuple[tuple[str, bytes], ...]
    removed: tuple[str, ...]
    replace: bool = False


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
    def weight_records(self) -> dict:
        """Last saved values and timestamps, before reversion, for model transparency."""
        return {r["axis"]: dict(r) for r in self.conn.execute(
            "SELECT axis, weight, updated_at FROM rec_weights")}

    @synchronized
    def get_weights(self, now=None, revert_halflife_d=60.0) -> dict:
        """Learned blend weights by axis (missing axis = prior 1.0), with time-proportional reversion
        toward 1.0 applied at read time (non-persisting: the stored value is untouched). An explicit
        genre mute is a categorical exclusion, not a soft preference, so zero never mean-reverts."""
        now = time.time() if now is None else now
        return {r["axis"]: (0.0 if r["axis"].startswith("genre:") and r["weight"] == 0.0
                            else _reverted(r["weight"], r["updated_at"], now, revert_halflife_d))
                for r in self.conn.execute("SELECT axis, weight, updated_at FROM rec_weights")}

    @synchronized
    def nudge_weight(self, axis, factor, lo=_WEIGHT_MIN, hi=_WEIGHT_MAX, now=None, revert_halflife_d=60.0) -> float:
        """Revert the stored weight for elapsed time since its last nudge, then multiply by factor
        (clamped), then persist the result with updated_at=now. Explicit genre mutes remain zero
        until a manual override unmutes them, regardless of reversion or the caller's floor."""
        now = time.time() if now is None else now
        row = self.conn.execute("SELECT weight, updated_at FROM rec_weights WHERE axis=?", (axis,)).fetchone()
        if row and axis.startswith("genre:") and row["weight"] == 0.0:
            return 0.0
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

    @synchronized
    def save_taste_settings(self, key, param_keys, now, replace=False) -> None:
        """Keep a recovery point, including raw weights and their original fade timestamps."""
        if not replace and self.conn.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone():
            return
        placeholders = ",".join("?" for _ in param_keys)
        snapshot = {
            "saved_at": now,
            "params": dict(self.conn.execute(
                f"SELECT key, value FROM settings WHERE key IN ({placeholders})", param_keys)),
            "weights": list(self.weight_records().values()),
        }
        self.conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                          (key, json.dumps(snapshot)))
        self.conn.commit()

    @synchronized
    def restore_taste_settings(self, key, param_keys) -> bool:
        raw = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if raw is None:
            return False
        snapshot = json.loads(raw["value"])
        from yt_playlist.core.recommendation_settings import LANE_NAMES
        weights = [w for w in snapshot["weights"]
                   if not w["axis"].startswith("lane:") or w["axis"][5:] in LANE_NAMES]
        with self.conn:
            self.conn.executemany("DELETE FROM settings WHERE key=?", [(k,) for k in param_keys])
            self.conn.executemany("INSERT INTO settings(key, value) VALUES (?, ?)",
                                  [(k, v) for k, v in snapshot["params"].items() if k in param_keys])
            self.conn.execute("DELETE FROM rec_weights")
            self.conn.executemany("INSERT INTO rec_weights(axis, weight, updated_at) VALUES (?, ?, ?)",
                                  [(w["axis"], w["weight"], w["updated_at"]) for w in weights])
        return True

    # --- feedback events (dismiss / less / more / mute / not_now) ---
    @synchronized
    def record_feedback(self, surface, item_key, kind, reason=None, scope="", until=None, now=None) -> None:
        """Persist a feedback event (dismiss/less/more/mute/not_now). Upserts per (surface,item,scope)."""
        item_key = catalog.resolve_key(self.conn,item_key) or item_key
        artist_id = None
        if item_key.startswith('artist:'):
            artist_id = artists.state_id(self.conn,item_key[7:])
        elif surface in ('artist','new_artists'):
            artist_id = artists.state_id(self.conn,item_key)
        self.conn.execute(
            "INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at,artist_id,target_type) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(surface,item_key,scope) DO UPDATE SET "
            "kind=excluded.kind, reason=excluded.reason, until=excluded.until, created_at=excluded.created_at, "
            "artist_id=excluded.artist_id,target_type=CASE WHEN excluded.artist_id IS NOT NULL "
            "THEN 'artist' ELSE rec_feedback.target_type END",
            (surface, item_key, kind, reason, scope or "", until, now, artist_id,
             'artist' if artist_id is not None else 'legacy_unresolved'))
        self.conn.commit()

    @synchronized
    def suppressed_keys(self, surface, now, scope="", *, keys=None) -> set:
        """Keys to hide on a surface: dismissed/muted/snoozed (surface-scoped) PLUS YouTube dislikes
        and explicit genre mutes (global, every surface). Feedback honors any 'until' expiry.

        A zero preference is an eligibility rule, including for unscored and cached candidates;
        lowering a score alone would still let muted tracks fill an otherwise sparse card.
        """
        keys = list(dict.fromkeys(keys)) if keys is not None else None
        if keys == []:
            return set()
        clause = (' AND item_key IN (' + ','.join('?' for _ in keys) + ')') if keys is not None else ''
        rows = self.conn.execute(
            "SELECT item_key FROM rec_feedback WHERE "
            "( (surface=? AND (scope='' OR scope=?) AND kind IN ('dismiss','mute','not_now')) "
            "  OR kind='dislike' ) "
            "AND (until IS NULL OR until>?)" + clause,
            (surface, scope or "", now, *(keys or []))).fetchall()
        blocked = {r["item_key"] for r in rows}
        blocked.update(r[0] for r in self.conn.execute(
            "SELECT DISTINCT r.identity_key FROM rec_feedback f "
            "JOIN track_artists ta ON ta.artist_id=f.artist_id "
            "JOIN catalog_entries e ON e.id=ta.entry_id JOIN recordings r ON r.id=e.recording_id "
            "WHERE f.kind='mute' AND (f.until IS NULL OR f.until>?)" +
            ((' AND r.identity_key IN (' + ','.join('?' for _ in keys) + ')') if keys is not None else ''),
            (now, *(keys or []))))
        muted_genres = {r["axis"][6:] for r in self.conn.execute(
            "SELECT axis FROM rec_weights WHERE axis LIKE 'genre:%' AND weight=0")}
        if muted_genres:
            keys = list(keys) if keys is not None else None
            if keys == []:
                return blocked
            clause = (" AND identity_key IN (" + ",".join("?" for _ in keys) + ")") if keys is not None else ""
            for row in self.conn.execute(
                    "SELECT identity_key,genre FROM tracks WHERE genre<>''" + clause +
                    " UNION SELECT identity_key,genre FROM discovered_tracks WHERE genre<>''" + clause,
                    (keys or []) * 2):
                if (genre_map.family(row["genre"]) in muted_genres
                        or genre_map.subgenre(row["genre"]) in muted_genres):
                    blocked.add(row["identity_key"])
        return blocked

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
    def apply_rating_feedback(self, states, *, dislike_seconds):
        from yt_playlist.core import rating_feedback

        with self.conn:
            return rating_feedback.apply(self.conn, states, dislike_seconds=dislike_seconds)

    @synchronized
    def record_dislike(self, identity_key, until, now) -> bool:
        """Persist a thumbs-down as a long global suppression (surface='sync'). Returns True iff newly
        created. The row feeds transient facets and durable negative vectors; re-sync never
        refreshes its evidence time or creates permanent genre/era preference changes."""
        return self._record_once("sync", identity_key, "dislike", until, now)

    @synchronized
    def clear_dislike(self, identity_key) -> None:
        """Reconcile an un-disliked track: drop its dislike row (un-suppress)."""
        self.conn.execute("DELETE FROM rec_feedback WHERE item_key=? AND kind='dislike'", (identity_key,))
        self.conn.commit()

    @synchronized
    def unban(self, identity_key, now) -> None:
        """Allow a recording locally until a new explicit confirmed dislike.

        Retained account/upload ratings stay intact. The typed feedback row
        follows verified recording reassociation and survives sync and restart.
        """
        identity_key = catalog.resolve_key(self.conn, identity_key) or identity_key
        recording = self.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (identity_key,)).fetchone()
        with self.conn:
            cutoff = self.conn.execute(
                "SELECT MAX(ir.observed_at) FROM item_ratings ir JOIN youtube_items y "
                "ON y.video_id=ir.video_id JOIN recordings r ON r.id=y.recording_id "
                "WHERE r.identity_key=? AND ir.source='api' AND ir.status='DISLIKE'",
                (identity_key,)).fetchone()[0]
            # A local allowance supersedes every retained command, even after
            # the wall clock moved backwards. A later command advances this cutoff.
            cutoff = max(now, cutoff) if cutoff is not None else now
            self.conn.execute("DELETE FROM rec_feedback WHERE item_key=? AND kind='dislike'", (identity_key,))
            self.conn.execute(
                'INSERT INTO rec_feedback(surface,item_key,kind,scope,created_at,target_type,recording_id) '
                "VALUES ('rating_override',?,'unban','',?,?,?) "
                'ON CONFLICT(surface,item_key,scope) DO UPDATE SET '
                'kind=excluded.kind,created_at=excluded.created_at,target_type=excluded.target_type,'
                'recording_id=excluded.recording_id',
                (identity_key, cutoff, 'recording' if recording else 'legacy_unresolved',
                 recording[0] if recording else None))

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

    # --- Likes (positive graduation signal; transient participation depends on provenance) ---
    # Provenance (stored in the like row's `reason` column):
    #   'action': observed live through the extension (player likeStatus or an observed rate call).
    #             The event time is real, so these keep full transient participation.
    #   'sync':   discovered by the library sync's bulk rated map. YTM reports no like-time, so
    #             created_at is only first-sight time; these must NOT feed the transient model and
    #             graduate into the permanent model only, at the low sync-like weight.
    @synchronized
    def record_like(self, identity_key, now, provenance="sync") -> bool:
        """Persist a like (surface='like', reason=provenance). Returns True iff newly created, so the
        caller feeds graduation exactly once. Idempotent on re-sync. If the like already exists with
        'sync' provenance and is re-observed as an 'action', the row is UPGRADED to action provenance
        (never downgraded) and False is still returned: an upgrade is not a new like."""
        row = self.conn.execute(
            "SELECT reason FROM rec_feedback WHERE surface='like' AND item_key=? AND scope='' "
            "AND kind='like'", (identity_key,)).fetchone()
        if row:
            if provenance == "action" and row["reason"] != "action":
                self.conn.execute(
                    "UPDATE rec_feedback SET reason='action' WHERE surface='like' AND item_key=? "
                    "AND scope='' AND kind='like'", (identity_key,))
                self.conn.commit()
            return False
        self.conn.execute(
            "INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at) "
            "VALUES ('like',?,'like',?,'',NULL,?)", (identity_key, provenance, now))
        self.conn.commit()
        return True

    @synchronized
    def recent_liked_keys(self, limit=None) -> list:
        """ALL liked identity_keys regardless of provenance, most-recent (created_at) first. For
        existence checks and counters (reconciliation, onboarding, taste page), NOT the transient
        model; the transient reader is recent_liked_with_ts, which is action-provenance only."""
        rows = self.conn.execute(
            "SELECT item_key FROM rec_feedback WHERE kind='like' ORDER BY created_at DESC").fetchall()
        keys = [r["item_key"] for r in rows]
        return keys[:limit] if limit else keys

    @synchronized
    def recent_liked_with_ts(self, limit=None) -> list:
        """#85 [(identity_key, created_at)] for ACTION-provenance likes, newest-first: the transient
        model's like feed. Sync-discovered likes are excluded here by design: they carry no true
        action timestamp (YTM's API and Takeout both lack like-time), so treating their first-sight
        created_at as an event time would let a re-recorded like masquerade as a fresh user action."""
        rows = self.conn.execute(
            "SELECT item_key, created_at FROM rec_feedback WHERE kind='like' AND reason='action' "
            "ORDER BY created_at DESC").fetchall()
        out = [(r["item_key"], float(r["created_at"])) for r in rows]
        return out[:limit] if limit else out

    @synchronized
    def like_provenance(self, identity_key):
        """'action' | 'sync' | None (no like row). Legacy rows stamped by the one-shot repair."""
        row = self.conn.execute(
            "SELECT reason FROM rec_feedback WHERE kind='like' AND item_key=? AND scope=''",
            (identity_key,)).fetchone()
        return row["reason"] if row else None

    @synchronized
    def mark_graduated_once(self, identity_key, kind, now) -> bool:
        """Once-EVER graduation stamp (kind='like_grad' | 'dislike_grad'; surface=kind, so the two
        signals stamp independently under rec_feedback's UNIQUE(surface, item_key, scope)).
        Invariant: a like/dislike may graduate its facets at most once per identity_key, ever. The
        stamp lives in rec_feedback but is intentionally independent of the like/dislike row
        lifecycle: clear_like/clear_dislike delete only their own kind, so a likeStatus flap (or a
        genuine unlike-then-relike) can never re-arm graduation. A provenance upgrade does not
        re-graduate either (record_like returns False on upgrade). Returns True iff this key has
        never graduated for this signal."""
        return self._record_once(kind, identity_key, kind, None, now)

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
        rows = self.conn.execute("SELECT item_key,artist_id FROM rec_feedback WHERE kind='mute'").fetchall()
        result = set()
        for row in rows:
            if row['artist_id']:
                ref = artists.token(row['artist_id'])
                name = self.conn.execute('SELECT name FROM artists WHERE id=?', (row['artist_id'],)).fetchone()[0]
                if artists.lookup(self.conn,name) == row['artist_id']:
                    result.add(name)
                else:
                    result.add(ref)
            elif row['item_key'].startswith('artist:'):
                result.add(row['item_key'][7:])
        return result

    @synchronized
    def feedback_summary(self) -> dict:
        """{kind: count} of stored feedback events, for the Taste Model page."""
        return {r["kind"]: r["c"] for r in self.conn.execute(
            "SELECT kind, COUNT(*) c FROM rec_feedback WHERE kind<>'unban' GROUP BY kind")}

    @synchronized
    def recent_rating_inputs(self, since, until) -> dict:
        """Retained rating inputs first added in [since, until], not an action ledger.

        Sync discovers existing ratings without knowing their action time. Keep those likes
        separate; dislikes likewise describe when evidence entered the model. Count each track
        once per kind even if more than one feedback surface retains it.
        """
        rows = self.conn.execute(
            "WITH inputs AS (SELECT kind,item_key,MAX(CASE WHEN reason='action' THEN 1 ELSE 0 END) action "
            "FROM rec_feedback "
            "WHERE kind IN ('like','dislike') AND created_at>=? AND created_at<=? "
            "GROUP BY kind,item_key) "
            "SELECT kind,CASE WHEN kind='like' AND action=1 THEN 'action' ELSE 'sync' END provenance,"
            "COUNT(*) n FROM inputs GROUP BY kind,provenance", (since, until)).fetchall()
        counts = {(r["kind"], r["provenance"]): r["n"] for r in rows}
        return {"likes": counts.get(("like", "action"), 0),
                "synced_likes": counts.get(("like", "sync"), 0),
                "dislikes": sum(n for (kind, _), n in counts.items() if kind == "dislike")}

    @synchronized
    def clear_feedback(self) -> None:
        """Clear visible feedback while keeping receipts for the learning we retain."""
        self.conn.execute("DELETE FROM rec_feedback WHERE kind NOT IN ('like_grad','dislike_grad')")
        self.conn.execute("DELETE FROM rec_impressions")
        self.conn.commit()

    # --- taste-embedding vectors ---
    @synchronized
    def next_feedback_expiry(self, now):
        row = self.conn.execute('SELECT MIN(until) FROM rec_feedback WHERE until>?', (now,)).fetchone()
        return row[0]

    @synchronized
    def prepare_training_changes(self, vectors):
        """Compare serialized results on the pinned reader, before taking the serving lock.

        These reads also register the output tables as snapshot dependencies, so a concurrent
        model publisher invalidates the delta just like a source edit invalidates its fit.
        """
        if not getattr(self._store, '_read_only', False):
            raise RuntimeError('Training changes require a read snapshot')
        changes = {}
        for table, rows in vectors.items():
            column = _VECTOR_TABLES.get(table)
            if column is None:
                raise ValueError('Invalid training model table')
            current = dict(self.conn.execute(f'SELECT {column},vec FROM {table}'))
            # Discovery encoding follows source order, unlike the sorted fitted spaces.
            # Preserve its tied rankings when a newly inserted key belongs between old rows.
            old_order = tuple(current) if table == 'rec_discovered_content_vectors' else None
            if old_order is not None:
                rows = tuple(rows)
            upserts, seen = [], set()
            for key, blob in rows:
                if key in seen:
                    raise ValueError('Duplicate training vector key')
                seen.add(key)
                if current.pop(key, None) != blob:
                    upserts.append((key, blob))
            if old_order is not None:
                old_keys = set(old_order)
                delta_order = [key for key in old_order if key in seen]
                delta_order.extend(key for key, _ in rows if key not in old_keys)
                if delta_order != [key for key, _ in rows]:
                    changes[table] = VectorChanges(rows, (), replace=True)
                    continue
            if upserts or current:
                changes[table] = VectorChanges(tuple(upserts), tuple(current))
        return changes

    @synchronized
    def publish_training_generation(self, changes):
        """Apply snapshot-checked deltas inside ReadSnapshot's atomic transaction.

        Identical rows avoid vector-page, revision-trigger and WAL churn unless preserving
        discovery order requires replacement. An omitted space stays intact; an empty target
        removes it.
        """
        if not self.conn.in_transaction:
            raise RuntimeError("Training publication requires an outer transaction")
        for table, delta in changes.items():
            column = _VECTOR_TABLES.get(table)
            if column is None:
                raise ValueError("Invalid training model table")
            if delta.replace:
                self.conn.execute(f'DELETE FROM {table}')
            self.conn.executemany(f'DELETE FROM {table} WHERE {column}=?',
                                  [(key,) for key in delta.removed])
            self.conn.executemany(f'INSERT INTO {table}({column},vec) VALUES (?,?) '
                f'ON CONFLICT({column}) DO UPDATE SET vec=excluded.vec', delta.upserts)

    @synchronized
    def replace_rec_vectors(self, rows) -> None:
        """Atomically replace all taste-embedding vectors. rows = iterable of (identity_key, bytes)."""
        self.conn.execute("DELETE FROM rec_vectors")
        self.conn.executemany("INSERT INTO rec_vectors(identity_key, vec) VALUES (?,?)", rows)
        self.conn.commit()

    @synchronized
    def get_rec_vectors(self) -> list[tuple]:
        # Match the fit's sorted keys even when a delta inserts between existing rows.
        return [(r["identity_key"], r["vec"])
                for r in self.conn.execute("SELECT identity_key, vec FROM rec_vectors ORDER BY identity_key")]

    @synchronized
    def rec_vectors_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM rec_vectors").fetchone()["c"]

    # --- content (genre/era) vectors: parallel space for the cluster blend ---
    @synchronized
    def content_generation(self, *, keys=None):
        """Read the encoding model and both vector pools from one SQLite snapshot."""
        self.conn.execute("SAVEPOINT content_snapshot")
        try:
            settings = {key: self._store.settings.get_setting(key) for key in (
                "rec_content_model", "rec_content_genre_revision", "rec_content_inputs",
                "rec_content_generation")}
            result = {
                "generation": settings.get("rec_content_generation") or "",
                "model": settings.get("rec_content_model"),
                "revision": settings.get("rec_content_genre_revision"),
                "inputs": settings.get("rec_content_inputs"),
                "library": (self.get_rec_content_vectors() if keys is None else
                            list(self.vectors_by_keys(keys, content=True).items())),
                "discovered": self.get_rec_discovered_content_vectors() if keys is None else [],
            }
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK TO content_snapshot")
                self.conn.execute("RELEASE content_snapshot")
            raise
        else:
            self.conn.execute("RELEASE content_snapshot")
        return result

    @synchronized
    def publish_content_generation(self, library, discovered, model, revision, inputs, expected_generation):
        """Publish a complete generation or leave the previous one intact.

        Compare the generation under the write lock so a slow discovery encoder cannot overwrite
        a newer library model. All computation and serialization happen before the first DELETE.
        """
        library, discovered = list(library), list(discovered)
        generation = uuid.uuid4().hex
        with self.conn:
            # Acquire SQLite's writer lock before the generation check, including across connections.
            self.conn.execute("UPDATE settings SET value=value WHERE key='rec_content_generation'")
            current = self.conn.execute(
                "SELECT value FROM settings WHERE key='rec_content_generation'").fetchone()
            if (current[0] if current else "") != expected_generation:
                return False
            for table, rows in (("rec_content_vectors", library),
                                ("rec_discovered_content_vectors", discovered)):
                self.conn.execute(f"DELETE FROM {table}")
                self.conn.executemany(f"INSERT INTO {table}(identity_key,vec) VALUES (?,?)", rows)
            self.conn.executemany("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", [
                ("rec_content_model", model), ("rec_content_genre_revision", revision),
                ("rec_content_inputs", inputs or ""), ("rec_content_generation", generation)])
        return True

    @synchronized
    def vectors_by_keys(self, keys, *, content=False) -> dict:
        """Read only the requested rows from one established model space."""
        keys = list(dict.fromkeys(keys))
        table = 'rec_content_vectors' if content else 'rec_vectors'
        result = {}
        for offset in range(0, len(keys), 500):
            batch = keys[offset:offset + 500]
            result.update(self.conn.execute(f'SELECT identity_key,vec FROM {table} WHERE identity_key IN ('
                + ','.join('?' for _ in batch) + ')', batch).fetchall())
        return result

    @synchronized
    def content_vector_pools(self, keys):
        """Keep library and discovery provenance separate for bounded encoder updates."""
        keys = list(dict.fromkeys(keys))
        pools = {'library': {}, 'discovered': {}}
        for scope, table in (('library', 'rec_content_vectors'),
                             ('discovered', 'rec_discovered_content_vectors')):
            for offset in range(0, len(keys), 500):
                batch = keys[offset:offset + 500]
                pools[scope].update(self.conn.execute(
                    f'SELECT identity_key,vec FROM {table} WHERE identity_key IN ('
                    + ','.join('?' for _ in batch) + ')', batch))
        return pools

    @synchronized
    def content_vectors_by_keys(self, keys) -> dict:
        """Read a bounded neighbourhood; prefer library vectors for overlapping recordings."""
        keys = list(dict.fromkeys(keys))
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        result = {}
        for table in ("rec_discovered_content_vectors", "rec_content_vectors"):
            result.update((r["identity_key"], r["vec"]) for r in self.conn.execute(
                f"SELECT identity_key,vec FROM {table} WHERE identity_key IN ({qs})", keys))
        return result

    @synchronized
    def replace_rec_content_vectors(self, rows) -> None:
        """Atomically replace all content vectors. rows = iterable of (identity_key, bytes)."""
        self.conn.execute("DELETE FROM rec_content_vectors")
        self.conn.executemany("INSERT INTO rec_content_vectors(identity_key, vec) VALUES (?,?)", rows)
        self.conn.commit()

    @synchronized
    def get_rec_content_vectors(self) -> list[tuple]:
        return [(r["identity_key"], r["vec"])
                for r in self.conn.execute("SELECT identity_key, vec FROM rec_content_vectors ORDER BY identity_key")]

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
                for r in self.conn.execute("SELECT artist, vec FROM rec_artist_vectors ORDER BY artist")]

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
                for r in self.conn.execute("SELECT artist, vec FROM rec_artist_content_vectors ORDER BY artist")]
