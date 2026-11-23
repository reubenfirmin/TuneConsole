"""ModesRepo: persistence for taste modes (issue #60, Part A).

One stable identity per discovered mode. Active rows carry content-space centroids
and display summaries; retired rows keep identity/labels for historical references.
Explicit preparation removes obsolete payloads. Use list_summaries for history;
list_modes returns None payloads for retired tombstones. Membership is derived.
"""
import json

import numpy as np

from yt_playlist.core import legacy_mode_payloads, mode_offers, mode_retention, recording_library
from yt_playlist.core.catalog_query import LIBRARY_TRACKS_SQL
from yt_playlist.repos.base import Repo, synchronized, lookup_read, snapshot_read


def _centroid_bytes(value, *, encoded=False):
    try:
        vector = np.frombuffer(value, dtype=np.float32) if encoded else np.asarray(value, dtype=np.float32)
    except (TypeError, ValueError) as error:
        raise ValueError('Mode centroid must be a nonempty finite vector') from error
    if vector.ndim != 1 or vector.size == 0 or not np.isfinite(vector).all():
        raise ValueError('Mode centroid must be a nonempty finite vector')
    return vector.tobytes()


class ModesRepo(Repo):
    @synchronized
    def publish_generation(self, rows, retired):
        """Commit prepared stable mode identities as one repository write."""
        rows = [(mid, label, families, _centroid_bytes(blob, encoded=True), *rest)
                for mid, label, families, blob, *rest in rows]
        with self.conn.atomic():
            legacy_mode_payloads.publish(self.conn, rows, retired)

    @snapshot_read
    def list_modes(self, active_only=True) -> list[dict]:
        sql = f"SELECT *,payload_retired_at IS NOT NULL AS payload_retired FROM {legacy_mode_payloads.VIEW}"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY size DESC, mode_id ASC"
        out = []
        for r in self.conn.execute(sql).fetchall():
            out.append({
                "mode_id": r["mode_id"],
                "label": r["label"],
                "families": None if r['payload_retired'] else json.loads(r["families"]),
                "centroid": None if r['payload_retired'] else np.frombuffer(r["centroid"], dtype=np.float32).copy(),
                "size": r["size"],
                "rep_keys": None if r['payload_retired'] else json.loads(r["rep_keys"]),
                "active": r["active"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
                "space": r["space"],
            })
        return out

    @snapshot_read
    def list_summaries(self, active_only=False) -> list[dict]:
        """Historical identity/labels without loading centroid or track payloads."""
        sql = f'SELECT mode_id,label,size,active,first_seen,last_seen,space FROM {legacy_mode_payloads.VIEW}'
        if active_only:
            sql += ' WHERE active=1'
        return [dict(row) for row in self.conn.execute(sql + ' ORDER BY size DESC,mode_id')]

    @synchronized
    def next_mode_id(self) -> int:
        row = self.conn.execute('SELECT MAX(legacy_mode_id) m FROM taste_groups').fetchone()
        high_water = self.conn.execute('SELECT value FROM settings WHERE key=?', (mode_retention.HIGH_WATER,)).fetchone()[0]
        return max(row["m"] or 0, int(high_water)) + 1

    @synchronized
    def replace_modes(self, upserts, retired_ids, now) -> None:
        upserts = list(upserts)
        if any(type(row['size']) is not int or row['size']<0 for row in upserts):
            raise ValueError('Mode size must be a nonnegative integer')
        now = float(now)
        rows = [(u['mode_id'], u['label'], json.dumps([list(f) for f in u['families']]),
                 _centroid_bytes(u['centroid']), u['size'], json.dumps(list(u['rep_keys'])), now, now, u.get('space') or '')
                for u in upserts]
        with self.conn.atomic(join=True):
            legacy_mode_payloads.publish(self.conn, rows, retired_ids)

    @synchronized
    def genres_for(self, keys) -> dict:
        if not keys:
            return {}
        q = ",".join("?" * len(keys))
        rows = self.conn.execute(
            f"SELECT identity_key, MIN(genre) genre FROM ({LIBRARY_TRACKS_SQL}) "
            f"WHERE identity_key IN ({q}) AND genre IS NOT NULL AND genre<>'' "
            f"GROUP BY identity_key", list(keys)).fetchall()
        return {r["identity_key"]: r["genre"] for r in rows}

    @synchronized
    def meta_for(self, keys) -> dict:
        if not keys:
            return {}
        q = ",".join("?" * len(keys))
        rows = self.conn.execute(
            f"SELECT identity_key, MIN(title) title, MIN(artist) artist FROM ({LIBRARY_TRACKS_SQL}) "
            f"WHERE identity_key IN ({q}) GROUP BY identity_key", list(keys)).fetchall()
        return {r["identity_key"]: {"title": r["title"], "artist": r["artist"]} for r in rows}

    @synchronized
    def years_for(self, keys) -> dict:
        """{identity_key: release_year_int} for tracks with a usable 4-digit mb_year (#63 temporal card)."""
        if not keys:
            return {}
        q = ",".join("?" * len(keys))
        library = recording_library.TRACKS_SQL
        rows = self.conn.execute(
            f"SELECT identity_key, MIN(mb_year) y FROM ({library}) "
            f"WHERE identity_key IN ({q}) AND mb_year GLOB '[0-9][0-9][0-9][0-9]*' "
            f"GROUP BY identity_key", list(keys)).fetchall()
        return {r["identity_key"]: int(r["y"][:4]) for r in rows}

    @synchronized
    def log_impressions(self, epoch, rows, now) -> None:
        """Record the first mode and ranker displayed in each lane for this rotation."""
        mode_offers.record(self.conn, epoch, rows, now)

    @synchronized
    def log_pick(self, membership_ref_id, mode_id, now, ranker=None) -> None:
        """Record a Save & play of a mode card, with the in-mode ranker it was served under (#57).
        Idempotent per membership_ref_id.

        This is MODE-LEVEL signal, deliberately exempt from the GENERATED_GROUP quarantine (see
        repos/base.py). Each generated playlist is built from one taste mode, so the user choosing to
        play it is a weak signal for that mode at the top of the model stack: it feeds the Thompson
        sampler through rec.mode_eval.mode_bandit_stats. It is evidence about the MODE, never about the
        tracks, which stay quarantined so the engine cannot feed on its own suggestions.

        Do not "fix" this by excluding generated playlists here. Every row in this table is, by
        construction, a play of a generated playlist. Quarantining it would delete the signal entirely.
        """
        with self.conn.atomic():
            # A historical caller may only retain the local handle. Keep its context
            # unknown; allocating future memberships must never reuse that handle.
            self.conn.execute('INSERT INTO main.playlists(id,is_current) VALUES (?,0) ON CONFLICT(id) DO NOTHING',
                              (int(membership_ref_id),))
            self.conn.execute(
                'UPDATE playlists SET picked_mode_id=?,picked_ranker=?,mode_picked_at=? '
                'WHERE id=? AND mode_picked_at IS NULL', (int(mode_id), ranker, float(now), int(membership_ref_id)))

    @lookup_read
    def epoch_started_at(self, epoch):
        """First display of this menu, used to defer its feedback until the next rotation."""
        return min((offer[3] for offer in mode_offers.offers(self.conn, int(epoch)).values()), default=None)

    @snapshot_read
    def impression_counts(self, since=None, *, before=None) -> dict:
        sql = f"SELECT mode_id, COUNT(*) c FROM {mode_offers.VIEW} WHERE mode_id IS NOT NULL"
        args = []
        if since is not None:
            sql += " AND created_at >= ?"
            args.append(float(since))
        if before is not None:
            sql += " AND created_at < ?"
            args.append(float(before))
        sql += " GROUP BY mode_id"
        return {r["mode_id"]: r["c"] for r in self.conn.execute(sql, args).fetchall()}

    @snapshot_read
    def pick_counts(self, since=None, *, before=None) -> dict:
        """Compact choice counts for the chart, sampler and measurement snapshots."""
        return {r["mode_id"]: r["n"] for r in self.conn.execute(
            "SELECT picked_mode_id AS mode_id,COUNT(*) n FROM playlists WHERE picked_mode_id IS NOT NULL "
            "AND (? IS NULL OR mode_picked_at>=?) AND (? IS NULL OR mode_picked_at<?) GROUP BY picked_mode_id",
            (since, since, before, before))}

    @synchronized
    def choice_learning_rows(self) -> list[dict]:
        """The sampler's active groups, counts and last evidence time, without loading vectors.

        Aggregate offers and picks separately so joining them cannot multiply either count.
        Keep orphan picks visible, but identify groups that have not been offered yet: their
        picks do not enter the sampler until an offer exists.
        """
        rows = self.conn.execute(
            "WITH offers AS (SELECT mode_id, COUNT(*) n, MAX(created_at) at "
            f"FROM {mode_offers.VIEW} GROUP BY mode_id), "
            "picks AS (SELECT picked_mode_id AS mode_id, COUNT(*) n, MAX(mode_picked_at) at "
            "FROM playlists WHERE picked_mode_id IS NOT NULL GROUP BY picked_mode_id) "
            "SELECT m.mode_id, m.label, COALESCE(o.n, 0) offered, "
            "COALESCE(p.n, 0) picked, "
            "CASE WHEN p.at IS NULL THEN o.at WHEN o.at IS NULL THEN p.at "
            "ELSE MAX(o.at, p.at) END updated_at "
            f"FROM {legacy_mode_payloads.VIEW} m LEFT JOIN offers o ON o.mode_id=m.mode_id "
            "LEFT JOIN picks p ON p.mode_id=m.mode_id "
            "WHERE m.active=1 AND (o.n IS NOT NULL OR p.n IS NOT NULL) "
            "ORDER BY updated_at DESC, m.mode_id").fetchall()
        return [dict(row) for row in rows]
