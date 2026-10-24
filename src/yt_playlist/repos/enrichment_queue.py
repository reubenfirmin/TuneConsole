"""Enrichment job selection, membership and pass completion.

Account-wide ranking uses a pinned reader; admission holds the serving lock for at most
64 candidates at a time. Completion/requeue writes commit on this repository's connection.
"""
import json
import time

from yt_playlist.core import catalog
from yt_playlist.core.db_diagnostics import database_operation
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos.enrichment_sql import TRACKS_CTE, eligible_providers, pending_coverage


class EnrichmentQueueRepo(Repo):
    @synchronized
    def mark_recordings_enriched(self, recording_ids, now):
        self.conn.executemany('UPDATE recordings SET first_enriched_at=COALESCE(first_enriched_at,?),last_enriched_at=? WHERE id=?',
                              [(now,now,rid) for rid in recording_ids])
        self.conn.commit()

    @synchronized
    def enrichment_memberships(self, recording_ids):
        ids = list(recording_ids)
        if not ids:
            return set()
        placeholders = ','.join('?' for _ in ids)
        scopes = set()
        if self.conn.execute(f'SELECT 1 FROM catalog_entries e JOIN library_memberships l ON l.entry_id=e.id WHERE e.recording_id IN ({placeholders}) LIMIT 1',ids).fetchone():
            scopes.add('library')
        if self.conn.execute(f'SELECT 1 FROM discovery_memberships WHERE recording_id IN ({placeholders}) LIMIT 1',ids).fetchone():
            scopes.add('discovery')
        return scopes

    @staticmethod
    def _queue_track(row):
        # Completion addresses the recording job; id identifies its exact input item/entry.
        return {"job_id": row["recording_id"], **{k: row[k] for k in ("recording_id", "source", "id", "identity_key", "video_id", "title", "artist", "last_enriched_at",
                                    "thumbnail", "mb_recording_id")
                if k != "id" or row[k] is not None}}

    # Track plays and the play sums of each track's playlist, album and artist containers.
    _QUEUE_CTES = """
        WITH plays AS (SELECT identity_key, COUNT(*) c FROM listening_days GROUP BY identity_key),
        tp AS (SELECT t.id,t.recording_id, COALESCE(p.c,0) sp FROM catalog_tracks t
               LEFT JOIN plays p ON p.identity_key=t.identity_key),
        pl_plays AS (SELECT playlist_id,SUM(sp) c FROM (
                     SELECT DISTINCT pt.playlist_id,tp.recording_id,tp.sp FROM playlist_tracks pt
                     JOIN tp ON tp.id=pt.track_id) GROUP BY playlist_id),
        al_plays AS (SELECT album_id,SUM(sp) c FROM (
                     SELECT DISTINCT at.album_id,tp.recording_id,tp.sp FROM album_tracks at
                     JOIN tp ON tp.id=at.entry_id) GROUP BY album_id),
        ar_plays AS (SELECT ar,SUM(sp) c FROM (
                     SELECT DISTINCT t.artist ar,tp.recording_id,tp.sp FROM catalog_tracks t
                     JOIN tp ON tp.id=t.id WHERE t.artist IS NOT NULL AND t.artist<>'') GROUP BY ar)
    """

    @synchronized
    def priority_enrich_batch(self, keys, limit, stale_before, *, providers=None, now=None):
        """Visible radio songs join the existing worker, with provider retry gates intact."""
        rows = self.conn.execute(
            "WITH " + TRACKS_CTE + " SELECT * FROM enrichment_tracks t "
            "WHERE identity_key IN (SELECT value FROM json_each(:keys)) "
            "AND (genre IS NULL OR genre='' OR mb_year IS NULL OR mb_year='') "
            "AND (first_enriched_at IS NULL OR last_enriched_at<:stale_before OR " +
            pending_coverage() + ") AND " + eligible_providers() +
            " ORDER BY (SELECT CAST(key AS INTEGER) FROM json_each(:keys) "
            "WHERE value=t.identity_key), queue_order LIMIT :limit",
            {"keys": json.dumps(list(dict.fromkeys(keys))[:96]), "limit": limit,
             "stale_before": stale_before, "providers": json.dumps(providers) if providers is not None else None,
             "now": time.time() if now is None else now}).fetchall()
        result = [self._queue_track(row) for row in rows]
        for row, track in zip(rows, result):
            jobs = self._store.enrichment_state.enrichment_provider_jobs(track["recording_id"])
            if jobs and row["first_enriched_at"] is not None:
                track["provider_jobs"] = jobs
        return result

    def next_enrich_batch(self, limit, offset=0, *, providers=None, now=None) -> list:
        """Rank new arrivals, played songs, played containers, then unplayed orphans.

        Recheck selected inputs against current membership, pass and provider state after
        calculating account-wide ranking on a pinned reader.
        """
        with database_operation("EnrichmentQueueRepo.next_enrich_batch"):
            if getattr(self._store, "_read_only", False):
                return self._select_enrich_batch(limit, offset, providers=providers, now=now)
            with self._store.read_snapshot() as snapshot:
                reader = snapshot.store.enrichment_queue
                configuration = reader._queue_provider_configuration(providers)
                candidates = reader._select_enrich_batch(limit, offset, providers=providers, now=now)
            result = []
            # Normal batches contain 40 tracks; larger diagnostic reads still yield the lock.
            for start in range(0, len(candidates), 64):
                result.extend(self._admit_enrich_batch(candidates[start:start + 64], configuration,
                                                      providers=providers, now=now))
            return result

    @synchronized
    def _queue_provider_configuration(self, providers):
        return [tuple(row) for row in self.conn.execute(
            "SELECT provider,fingerprint,status FROM enrichment_provider_config "
            "WHERE :providers IS NULL OR provider IN (SELECT value FROM json_each(:providers)) "
            "ORDER BY provider", {"providers": json.dumps(providers) if providers is not None else None})]

    @synchronized
    def _admit_enrich_batch(self, candidates, configuration, *, providers, now):
        # A changed configuration can replace the same recording/provider job. Defer it.
        if self._queue_provider_configuration(providers) != configuration:
            return []
        rows = self.conn.execute(
            "WITH " + TRACKS_CTE + " SELECT t.* FROM json_each(:candidates) selected "
            # Constrain the view's outer recording PK to avoid scanning all subjects per input.
            "JOIN enrichment_tracks t ON t.queue_order=json_extract(selected.value,'$.recording_id') "
            "AND t.recording_id=t.queue_order "
            "AND t.id IS json_extract(selected.value,'$.id') "
            "AND t.last_enriched_at IS json_extract(selected.value,'$.last_enriched_at') "
            "WHERE (t.first_enriched_at IS NULL OR " + pending_coverage() + ") AND " +
            eligible_providers(coverage=True) + " ORDER BY CAST(selected.key AS INTEGER)",
            {"candidates": json.dumps([{key: track.get(key) for key in
                                        ("recording_id", "id", "last_enriched_at")} for track in candidates]),
             "providers": json.dumps(providers) if providers is not None else None,
             "now": time.time() if now is None else now}).fetchall()
        return [self._queue_track(row) for row in rows]

    @synchronized
    def _select_enrich_batch(self, limit, offset=0, *, providers=None, now=None):
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key='enrich_caught_up_at'").fetchone()
        caught_up = float(row["value"]) if row and row["value"] else 0.0
        rows = self.conn.execute(self._QUEUE_CTES + ", " + TRACKS_CTE + """,
          queue AS (
            SELECT t.*, COALESCE(p.c, 0) sp,
              MAX(COALESCE((SELECT MAX(plp.c) FROM playlist_tracks pt JOIN pl_plays plp
                            ON plp.playlist_id=pt.playlist_id WHERE pt.track_id=t.id), 0),
                  COALESCE((SELECT MAX(ap.c) FROM al_plays ap JOIN album_tracks at ON at.album_id=ap.album_id
                            JOIN catalog_entries e ON e.id=at.entry_id WHERE e.recording_id=t.recording_id), 0),
                  COALESCE((SELECT c FROM ar_plays WHERE ar=t.artist), 0)) AS best
            FROM enrichment_tracks t LEFT JOIN plays p ON p.identity_key=t.identity_key
            WHERE (t.first_enriched_at IS NULL OR """ + pending_coverage() + ") AND " + eligible_providers(coverage=True) + """
          )
            SELECT * FROM queue
            ORDER BY
              COALESCE((SELECT MAX(s.at) FROM enrichment_provider_state s
                WHERE s.recording_id=queue.recording_id AND s.cycle=COALESCE(queue.last_enriched_at,-1)),0),
              CASE WHEN created_at IS NOT NULL AND created_at > :caught_up THEN 0
                   WHEN sp > 0 THEN 1
                   WHEN best > 0 THEN 2 ELSE 3 END ASC,
              CASE WHEN created_at IS NOT NULL AND created_at > :caught_up THEN -created_at
                   WHEN sp > 0 THEN -sp ELSE -best END ASC,
              created_at DESC, queue_order ASC, source ASC
            LIMIT :limit OFFSET :offset""", {"caught_up": caught_up, "limit": limit, "offset": offset,
                "providers": json.dumps(providers) if providers is not None else None,
                "now": time.time() if now is None else now}).fetchall()
        return [self._queue_track(r) for r in rows]

    @synchronized
    def resweep_batch(self, limit, stale_before, *, providers=None, now=None) -> list:
        """Processed subjects missing any field, oldest pass first, one attempt per stale window."""
        rows = self.conn.execute(
            "WITH " + TRACKS_CTE + " SELECT * FROM enrichment_tracks t "
            "WHERE first_enriched_at IS NOT NULL AND last_enriched_at < :stale_before "
            "AND (genre IS NULL OR genre='' OR mb_year IS NULL OR mb_year='' "
            "     OR bpm IS NULL OR energy IS NULL OR danceability IS NULL) "
            "AND NOT " + pending_coverage() + " "
            "AND " + eligible_providers() +
            " ORDER BY COALESCE((SELECT MAX(s.at) FROM enrichment_provider_state s "
            "WHERE s.recording_id=t.recording_id AND s.cycle=COALESCE(t.last_enriched_at,-1)),0), "
            "last_enriched_at ASC, queue_order ASC, source ASC LIMIT :limit",
            {"stale_before": stale_before, "limit": limit,
             "providers": json.dumps(providers) if providers is not None else None,
             "now": time.time() if now is None else now}).fetchall()
        return [self._queue_track(r) for r in rows]

    @synchronized
    def mark_enriched(self, track_ids, now) -> None:
        """Set first_enriched_at once and bump last_enriched_at on every processed pass."""
        if not track_ids:
            return
        track_ids = [catalog.resolve_entry(self.conn,t) for t in track_ids]
        qs = ",".join("?" * len(track_ids))
        self.conn.execute(
            f"UPDATE catalog_tracks SET first_enriched_at = COALESCE(first_enriched_at, ?), "
            f"last_enriched_at = ? WHERE id IN ({qs})", [now, now, *track_ids])
        self.conn.commit()

    @synchronized
    def requeue_enrichment(self, track_ids) -> None:
        """Fresh user intent retries incomplete tracks before the slow stale sweep."""
        track_ids = list(dict.fromkeys(catalog.resolve_entry(self.conn,t) for t in track_ids))
        if not track_ids:
            return
        qs = ",".join("?" * len(track_ids))
        self.conn.execute(
            f"UPDATE catalog_tracks SET first_enriched_at=NULL, last_enriched_at=NULL WHERE id IN ({qs})",
            track_ids)
        self.conn.commit()
