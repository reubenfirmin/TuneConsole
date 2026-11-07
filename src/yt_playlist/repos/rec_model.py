"""RecModelRepo: the learned taste model: blend weights, feedback events, and embedding vectors.

Owns taste weights, feedback and canonical recording/artist embeddings.
Split out of the former monolithic RecRepo so each rec concern is its own focused DAO.
"""
import json
import time
import uuid
from dataclasses import dataclass, replace

from yt_playlist.repos.base import Repo, synchronized, lookup_read, snapshot_read
from yt_playlist.core import artists, catalog, collection_recovery, embeddings, genre_identity, policy_documents, taste_axes, taste_control_state
from yt_playlist.core.db_revisions import column_group_reads

# Learned blend weights live in [_WEIGHT_MIN, _WEIGHT_MAX] around the 1.0 prior: the floor stops a run
# of negative feedback from disabling an axis outright (a 0 weight would never win the for_you fair
# queue), the ceiling stops one axis from swamping the blend. Weights mean-revert toward 1.0 over time
# (see _reverted below) rather than shrinking a flat amount on every nudge, so stale learning decays
# but reinforced preferences hold.
_WEIGHT_MIN, _WEIGHT_MAX = 0.2, 3.0

@dataclass(frozen=True)
class VectorChanges:
    upserts: tuple[tuple[str, bytes], ...]
    removed: tuple[str, ...]
    replace: bool = False
    model_version: str | None = None
    publication: embeddings.Publication | None = None


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
    @lookup_read
    def weight_records(self) -> dict:
        """Last saved values and timestamps, before reversion, for model transparency."""
        return {r["axis"]: dict(r) for r in self.conn.execute(
            taste_axes.projection(self.conn, 'rec_weights', 'axis,weight,updated_at'))}

    @lookup_read
    def get_weights(self, now=None, revert_halflife_d=60.0) -> dict:
        """Learned blend weights by axis (missing axis = prior 1.0), with time-proportional reversion
        toward 1.0 applied at read time (non-persisting: the stored value is untouched). An explicit
        genre mute is a categorical exclusion, not a soft preference, so zero never mean-reverts."""
        now = time.time() if now is None else now
        return {r["axis"]: (0.0 if r["axis"].startswith("genre:") and r["weight"] == 0.0
                            else _reverted(r["weight"], r["updated_at"], now, revert_halflife_d))
                for r in self.weight_records().values()}

    @synchronized
    def nudge_weight(self, axis, factor, lo=_WEIGHT_MIN, hi=_WEIGHT_MAX, now=None, revert_halflife_d=60.0) -> float:
        """Revert the stored weight for elapsed time since its last nudge, then multiply by factor
        (clamped), then persist the result with updated_at=now. Explicit genre mutes remain zero
        until a manual override unmutes them, regardless of reversion or the caller's floor."""
        now = time.time() if now is None else now
        column = taste_axes.column(self.conn)
        reference = taste_axes.reference(self.conn, 'rec_weights', axis, create=True)
        row = self.conn.execute(f"SELECT weight, updated_at FROM rec_weights WHERE {column}=?", (reference,)).fetchone()
        if row and axis.startswith("genre:") and row["weight"] == 0.0:
            return 0.0
        base = _reverted(row["weight"], row["updated_at"], now, revert_halflife_d) if row else 1.0
        w = max(lo, min(hi, base * factor))
        self.conn.execute(f"INSERT INTO rec_weights({column}, weight, updated_at) VALUES (?, ?, ?) "
                          f"ON CONFLICT({column}) DO UPDATE SET weight=excluded.weight, updated_at=excluded.updated_at",
                          (reference, w, now))
        self.conn.commit()
        return w

    @synchronized
    def set_weight(self, axis, weight, lo=_WEIGHT_MIN, hi=_WEIGHT_MAX, now=None) -> None:
        """Manual override (Taste Model page). Clamped to the same [lo, hi] band as nudge_weight so a
        stray 0/negative can't silently disable a for_you lane (its fair-queue ratio would never win).
        A manual override is fresh evidence; it re-stamps the reversion clock."""
        now = time.time() if now is None else now
        w = max(lo, min(hi, float(weight)))
        column = taste_axes.column(self.conn)
        reference = taste_axes.reference(self.conn, 'rec_weights', axis, create=True)
        self.conn.execute(f"INSERT INTO rec_weights({column}, weight, updated_at) VALUES (?, ?, ?) "
                          f"ON CONFLICT({column}) DO UPDATE SET weight=excluded.weight, updated_at=excluded.updated_at",
                          (reference, w, now))
        self.conn.commit()

    @synchronized
    def reset_weights(self) -> None:
        self.conn.execute("DELETE FROM rec_weights")
        self.conn.commit()

    @synchronized
    def save_taste_settings(self, key, param_keys, now, replace=False) -> None:
        """Keep a recovery point, including raw weights and their original fade timestamps."""
        with self.conn:
            typed = taste_control_state.available(self.conn)
            if typed:
                if key!=taste_control_state.CHECKPOINT:
                    raise ValueError('Use the retained taste settings checkpoint owner')
                self.conn.execute('UPDATE taste_settings_checkpoints SET revision=revision WHERE 0')
                exists = self.conn.execute('SELECT 1 FROM taste_settings_checkpoints').fetchone()
            else:
                self.conn.execute('UPDATE settings SET value=value WHERE 0')
                exists = self.conn.execute('SELECT 1 FROM settings WHERE key=?',(key,)).fetchone()
            if not replace and exists:
                return
            placeholders = ",".join("?" for _ in param_keys)
            snapshot = {
                "saved_at": now,
                "params": dict(self.conn.execute(
                    f"SELECT key, value FROM settings WHERE key IN ({placeholders})", param_keys)),
                "weights": list(self.weight_records().values()),
            }
            if typed:
                taste_control_state.put(self.conn,{key:json.dumps(snapshot)})
            else:
                self.conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                                  (key, json.dumps(snapshot)))

    @synchronized
    def restore_taste_settings(self, key, param_keys) -> bool:
        with policy_documents.retained_reader(collection_recovery.directory(self.conn)), self.conn:
            if taste_control_state.available(self.conn) and key!=taste_control_state.CHECKPOINT:
                raise ValueError('Use the retained taste settings checkpoint owner')
            self.conn.execute('UPDATE rec_weights SET weight=weight WHERE 0')
            raw = self._store.settings.get_setting(key)
            if raw is None:
                return False
            snapshot = json.loads(raw)
            from yt_playlist.core.recommendation_settings import LANE_NAMES
            weights = [w for w in snapshot["weights"]
                       if not w["axis"].startswith("lane:") or w["axis"][5:] in LANE_NAMES]
            self.conn.executemany("DELETE FROM settings WHERE key=?", [(k,) for k in param_keys])
            self.conn.executemany("INSERT INTO settings(key, value) VALUES (?, ?)",
                                  [(k, v) for k, v in snapshot["params"].items() if k in param_keys])
            self.conn.execute("DELETE FROM rec_weights")
            column = taste_axes.column(self.conn)
            restored = (taste_control_state.restored_weights(self.conn) if taste_control_state.available(self.conn) else
                        [(taste_axes.reference(self.conn,'rec_weights',w["axis"],create=True),w['weight'],w['updated_at']) for w in weights])
            self.conn.executemany(f"INSERT INTO rec_weights({column}, weight, updated_at) VALUES (?, ?, ?)",restored)
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

    @snapshot_read
    def suppressed_keys(self, surface, now, scope="", *, keys=None) -> set:
        """Keys to hide on a surface: dismissed/muted/snoozed (surface-scoped) PLUS YouTube dislikes
        and explicit genre mutes and temporary skip breaks (global, every surface).
        Feedback honors any 'until' expiry; skip breaks expire from raw exit history.

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
        blocked.update(self._store.player_events.skip_blackouts(now, keys=keys))
        blocked.update(r[0] for r in self.conn.execute(
            "SELECT DISTINCT r.identity_key FROM rec_feedback f "
            f"JOIN {self.entry_artists_table} ta ON ta.artist_id=f.artist_id "
            "JOIN catalog_entries e ON e.id=ta.entry_id JOIN recordings r ON r.id=e.recording_id "
            "WHERE f.kind='mute' AND (f.until IS NULL OR f.until>?)" +
            ((' AND r.identity_key IN (' + ','.join('?' for _ in keys) + ')') if keys is not None else ''),
            (now, *(keys or []))))
        muted_genres = {r['axis'][6:] for r in self.weight_records().values()
                        if r['axis'].startswith('genre:') and r['weight'] == 0}
        if muted_genres:
            keys = list(keys) if keys is not None else None
            if keys == []:
                return blocked
            clause = (" AND identity_key IN (" + ",".join("?" for _ in keys) + ")") if keys is not None else ""
            for row in self.conn.execute(
                    "SELECT identity_key,genre FROM tracks WHERE genre<>''" + clause +
                    " UNION SELECT identity_key,genre FROM discovered_tracks WHERE genre<>''" + clause,
                    (keys or []) * 2):
                if (genre_identity.family(self.conn, row['genre']) in muted_genres
                        or genre_identity.resolve(self.conn, row['genre'])[0] in muted_genres):
                    blocked.add(row["identity_key"])
        return blocked

    def _record_once(self, surface, item_key, kind, until, now) -> bool:
        """Insert a first-seen feedback row (scope=''), idempotently. Returns True iff newly created,
        so graduation can feed a recording's facets exactly once."""
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

    @snapshot_read
    def disliked_identity_keys(self) -> set:
        return {r["item_key"] for r in self.conn.execute(
            "SELECT item_key FROM rec_feedback WHERE kind='dislike'")}

    @lookup_read
    def disliked_key_page(self, *, after=None, limit=128):
        """Distinct evidence keys in stable bounded pages, including duplicate surfaces."""
        if (after is not None and not isinstance(after, str)
                or type(limit) is not int or not 1 <= limit <= 512):
            raise ValueError('Invalid bounded dislike page')
        predicate = ' AND item_key>?' if after is not None else ''
        return [row[0] for row in self.conn.execute(
            "SELECT DISTINCT item_key FROM rec_feedback WHERE kind='dislike'" + predicate +
            ' ORDER BY item_key LIMIT ?', (*((after,) if after is not None else ()), limit))]

    @snapshot_read
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
    @snapshot_read
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
        lifecycle: rating projection deletes only rating rows, so a likeStatus flap (or a
        genuine unlike-then-relike) can never re-arm graduation. A provenance upgrade does not
        re-graduate either. Returns True iff this key has
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

    # --- Standing slider leans (rec_lean): non-decaying transient multipliers, centered at 1.0 ---
    @synchronized
    def set_lean(self, axis, value, now) -> None:
        """Upsert a standing lean (home-slider position). value is a multiplier; 1.0 = neutral."""
        column = taste_axes.column(self.conn)
        reference = taste_axes.reference(self.conn, 'rec_lean', axis, create=True)
        self.conn.execute(
            f"INSERT INTO rec_lean({column}, value, updated_at) VALUES (?,?,?) "
            f"ON CONFLICT({column}) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (reference, float(value), now))
        self.conn.commit()

    @lookup_read
    def get_leans(self) -> dict:
        """All standing leans {axis: value}. Absence of an axis means neutral 1.0."""
        return {r["axis"]: r["value"] for r in self.conn.execute(
            taste_axes.projection(self.conn,'rec_lean','axis,value'))}

    @lookup_read
    def get_lean(self, axis) -> float:
        row = taste_axes.lookup(self.conn,'rec_lean',axis,'value')
        return row["value"] if row else 1.0

    @lookup_read
    def lean_rows(self) -> list:
        return self.conn.execute(taste_axes.projection(
            self.conn,'rec_lean','axis,value,updated_at,last_graduated_day')).fetchall()

    @synchronized
    def set_lean_graduated_day(self, axis, day) -> None:
        self.conn.execute(f"UPDATE rec_lean SET last_graduated_day=? WHERE {taste_axes.column(self.conn)}=?",
                          (day, taste_axes.reference(self.conn,'rec_lean',axis)))
        self.conn.commit()

    @synchronized
    def clear_lean(self, axis) -> None:
        self.conn.execute(f"DELETE FROM rec_lean WHERE {taste_axes.column(self.conn)}=?",
                          (taste_axes.reference(self.conn,'rec_lean',axis),))
        self.conn.commit()

    @synchronized
    def clear_all_leans(self) -> None:
        """Wipe every standing lean (Home 'Reset to default', bars back to neutral). Does NOT touch
        permanent weights (those are the long-term taste model, edited on the Taste page)."""
        self.conn.execute("DELETE FROM rec_lean")
        self.conn.commit()

    # --- Home bar curation (home_hidden_facet): which steering bars to SHOW; not a taste signal ---
    @lookup_read
    def hidden_facets(self) -> set:
        """Axes the user removed from the Home panel (display-only)."""
        return {r["axis"] for r in self.conn.execute(taste_axes.projection(self.conn,'home_hidden_facet','axis'))}

    @synchronized
    def hide_facet(self, axis) -> None:
        self.conn.execute(f"INSERT OR IGNORE INTO home_hidden_facet({taste_axes.column(self.conn)}) VALUES (?)",
                          (taste_axes.reference(self.conn,'home_hidden_facet',axis,create=True),))
        self.conn.commit()

    @synchronized
    def unhide_facet(self, axis) -> None:
        predicate,parameters = taste_axes.matches(self.conn,'home_hidden_facet',axis)
        self.conn.execute('DELETE FROM home_hidden_facet WHERE '+predicate,parameters)
        self.conn.commit()

    @synchronized
    def clear_hidden_facets(self) -> None:
        self.conn.execute("DELETE FROM home_hidden_facet")
        self.conn.commit()

    @lookup_read
    def muted_album_ids(self, browse_ids, now) -> set:
        """Current artist mutes over canonical credits of a bounded release cohort."""
        browse_ids = list(dict.fromkeys(browse_ids))
        if not browse_ids:
            return set()
        return {row[0] for row in self.conn.execute(
            "SELECT DISTINCT p.provider_id FROM album_provider_ids p "
            "JOIN album_artists a ON a.album_id=p.album_id "
            "JOIN rec_feedback f ON f.artist_id=a.artist_id "
            "WHERE p.provider='youtube' AND f.kind='mute' AND (f.until IS NULL OR f.until>?) "
            "AND p.provider_id IN (" + ','.join('?' for _ in browse_ids) + ")",
            (now, *browse_ids))}

    @snapshot_read
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
    @lookup_read
    def collaborative_basis(self):
        return embeddings.head(self.conn, 'rec_vectors')

    @synchronized
    def next_feedback_expiry(self, now):
        row = self.conn.execute('SELECT MIN(until) FROM rec_feedback WHERE until>?', (now,)).fetchone()
        return row[0]

    @synchronized
    def prepare_training_changes(self, vectors, settings=None):
        """Compare serialized results on the pinned reader, before taking the serving lock.

        These reads also register the output tables as snapshot dependencies, so a concurrent
        model publisher invalidates the delta just like a source edit invalidates its fit.
        """
        if not getattr(self._store, '_read_only', False):
            raise RuntimeError('Training changes require a read snapshot')
        basis_settings = self._embedding_settings(settings)
        changes = {}
        for table, rows in vectors.items():
            embeddings.descriptor(table)
            with column_group_reads(self._store,'recordings','identity'):
                current = dict(embeddings.rows(self.conn, table))
            head = embeddings.head(self.conn, table)
            version = embeddings.version(table, basis_settings)
            rows = tuple(rows)
            if len({key for key, _ in rows}) != len(rows):
                raise ValueError('Duplicate training vector key')
            dimension = embeddings.model_dimensions(table, basis_settings)
            if dimension is not None and any(embeddings.dimensions(blob) != dimension for _key, blob in rows):
                raise ValueError('Embedding dimensions do not match their encoder')
            if version and head and version != head[0]:
                changes[table] = VectorChanges(rows, (), replace=True, model_version=version)
                continue
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
                    changes[table] = VectorChanges(rows, (), replace=True, model_version=version)
                    continue
            if upserts or current:
                changes[table] = VectorChanges(tuple(upserts), tuple(current),
                                              model_version=version if head is None else None)
        return self.prepare_training_deltas(changes)

    @synchronized
    def prepare_training_deltas(self, changes):
        """Materialize a bounded incremental publication on its pinned reader."""
        if not getattr(self._store, '_read_only', False):
            raise RuntimeError('Training changes require a read snapshot')
        with column_group_reads(self._store, 'recordings', 'identity'):
            return {table: replace(delta, publication=embeddings.prepare(
                self.conn, table, delta.upserts, delta.removed,
                replace=delta.replace, model_version=delta.model_version))
                for table, delta in changes.items()}

    def _embedding_settings(self, overrides=None):
        values = {key: self._store.settings.get_setting(key) for key in (
            'rec_content_model', 'rec_artist_content_model', 'rec_fit_inputs',
            'rec_artist_fit_inputs', 'rec_embed_method', 'rec_dim')}
        values.update(overrides or {})
        return values

    def _replace_embeddings(self, name, rows):
        # Reserve the writer before validating owners and choosing the basis.
        with self.conn:
            self.conn.execute('UPDATE settings SET value=value WHERE 0')
            rows = tuple(rows)
            settings = self._embedding_settings()
            dimension = embeddings.model_dimensions(name, settings)
            if dimension is not None and any(embeddings.dimensions(blob) != dimension for _key, blob in rows):
                raise ValueError('Embedding dimensions do not match their encoder')
            embeddings.publish(self.conn, name, rows, replace=True,
                               model_version=embeddings.version(name, settings))

    @synchronized
    def publish_training_generation(self, changes):
        """Commit snapshot-checked deltas within this repository.

        Identical rows avoid vector-page, revision-trigger and WAL churn unless preserving
        discovery order requires replacement. An omitted space stays intact; an empty target
        removes it.
        """
        for table in changes:
            embeddings.descriptor(table)
        with self.conn.atomic(join=True):
            for table, delta in changes.items():
                if delta.publication is None:
                    embeddings.publish(self.conn, table, delta.upserts, delta.removed,
                                       replace=delta.replace, model_version=delta.model_version)
                else:
                    if delta.publication.name != table:
                        raise ValueError('Training publication belongs to another space')
                    embeddings.publish_prepared(self.conn, delta.publication)

    @synchronized
    def replace_rec_vectors(self, rows) -> None:
        """Atomically replace all taste-embedding vectors. rows = iterable of (identity_key, bytes)."""
        self._replace_embeddings('rec_vectors', rows)

    @synchronized
    def get_rec_vectors(self) -> list[tuple]:
        # Match the fit's sorted keys even when a delta inserts between existing rows.
        return embeddings.rows(self.conn, 'rec_vectors')

    @synchronized
    def rec_vectors_count(self) -> int:
        return embeddings.count(self.conn, 'rec_vectors')

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
                "library_basis": embeddings.head(self.conn, 'rec_content_vectors'),
                "discovered_basis": embeddings.head(self.conn, 'rec_discovered_content_vectors'),
                "library": (self.get_rec_content_vectors() if keys is None else
                            list(self.vectors_by_keys(keys, content=True).items())),
                "discovered": self.get_rec_discovered_content_vectors() if keys is None else [],
            }
            expected = embeddings.version('rec_content_vectors', settings)
            result['coherent'] = True
            for scope in ('library', 'discovered'):
                basis = result[scope + '_basis']
                if expected and basis and basis[0] != expected:
                    result[scope] = []
                    result['coherent'] = False
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
            current = self._store.settings.get_setting('rec_content_generation') or ''
            if current != expected_generation:
                return False
            for table, rows in (("rec_content_vectors", library),
                                ("rec_discovered_content_vectors", discovered)):
                dimension = embeddings.model_dimensions(table, {'rec_content_model': model})
                if any(embeddings.dimensions(blob) != dimension for _key, blob in rows):
                    raise ValueError('Embedding dimensions do not match their encoder')
                embeddings.publish(self.conn, table, rows, replace=True,
                    model_version=embeddings.version(table, {'rec_content_model': model}))
            self._store.settings.publish_values(dict([
                ("rec_content_model", model), ("rec_content_genre_revision", revision),
                ("rec_content_inputs", inputs or ""), ("rec_content_generation", generation)]))
        return True

    @synchronized
    def vectors_by_keys(self, keys, *, content=False) -> dict:
        """Read only the requested rows from one established model space."""
        if content:
            return self.content_vector_pools(keys)['library']
        keys = list(dict.fromkeys(keys))
        table = 'rec_vectors'
        result = {}
        for offset in range(0, len(keys), 500):
            batch = keys[offset:offset + 500]
            result.update(embeddings.rows(self.conn, table, keys=batch))
        return result

    @synchronized
    def content_vector_pools(self, keys):
        """Read bounded, compatible cohorts and their encoder from one snapshot."""
        keys = list(dict.fromkeys(keys))
        pools = {'library': {}, 'discovered': {}}
        self.conn.execute('SAVEPOINT content_vectors_snapshot')
        try:
            expected = embeddings.version('rec_content_vectors',
                {'rec_content_model': self._store.settings.get_setting('rec_content_model')})
            tables = {'library': 'rec_content_vectors', 'discovered': 'rec_discovered_content_vectors'}
            heads = {scope: embeddings.head(self.conn, table) for scope, table in tables.items()}
            for scope, table in tables.items():
                basis = heads[scope]
                if not basis or (expected and basis[0] != expected):
                    continue
                if not expected and scope == 'discovered' and heads['library'] and basis != heads['library']:
                    continue
                for offset in range(0, len(keys), 500):
                    pools[scope].update(embeddings.rows(self.conn, table, keys=keys[offset:offset + 500]))
        finally:
            self.conn.execute('RELEASE content_vectors_snapshot')
        return pools

    @synchronized
    def content_vectors_by_keys(self, keys) -> dict:
        """Read a bounded neighbourhood; prefer library vectors for overlapping recordings."""
        pools = self.content_vector_pools(keys)
        return {**pools['discovered'], **pools['library']}

    @synchronized
    def replace_rec_content_vectors(self, rows) -> None:
        """Atomically replace all content vectors. rows = iterable of (identity_key, bytes)."""
        self._replace_embeddings('rec_content_vectors', rows)

    @synchronized
    def get_rec_content_vectors(self) -> list[tuple]:
        return embeddings.rows(self.conn, 'rec_content_vectors')

    @synchronized
    def rec_content_vectors_count(self) -> int:
        return embeddings.count(self.conn, 'rec_content_vectors')

    @synchronized
    def rec_discovered_content_vectors_count(self) -> int:
        return embeddings.count(self.conn, 'rec_discovered_content_vectors')

    @synchronized
    def replace_rec_discovered_content_vectors(self, rows) -> None:
        """Atomically replace out-of-corpus (discovered) content vectors. rows = (identity_key, bytes)."""
        self._replace_embeddings('rec_discovered_content_vectors', rows)

    @synchronized
    def get_rec_discovered_content_vectors(self) -> list[tuple]:
        return embeddings.rows(self.conn, 'rec_discovered_content_vectors')

    # --- Artist spaces use canonical IDs; public keys remain @artist:<id>. ---
    @synchronized
    def replace_rec_artist_vectors(self, rows) -> None:
        """Atomically replace all collaborative artist vectors. rows = iterable of (artist, bytes)."""
        self._replace_embeddings('rec_artist_vectors', rows)

    @synchronized
    def get_rec_artist_vectors(self) -> list[tuple]:
        return embeddings.rows(self.conn, 'rec_artist_vectors')

    @synchronized
    def rec_artist_vectors_count(self) -> int:
        return embeddings.count(self.conn, 'rec_artist_vectors')

    @synchronized
    def replace_rec_artist_content_vectors(self, rows) -> None:
        """Atomically replace all artist content vectors. rows = iterable of (artist, bytes)."""
        self._replace_embeddings('rec_artist_content_vectors', rows)

    @synchronized
    def get_rec_artist_content_vectors(self) -> list[tuple]:
        return embeddings.rows(self.conn, 'rec_artist_content_vectors')
