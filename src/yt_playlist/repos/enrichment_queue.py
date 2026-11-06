"""SQL selection, current-input admission and completion for enrichment jobs."""
import json

from yt_playlist.core import catalog
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.db_revisions import column_group_reads
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos.enrichment_sql import TRACKS_CTE


_QUEUE_FIELDS = ("recording_id", "source", "id", "identity_key", "video_id", "title", "artist",
                 "last_enriched_at", "thumbnail", "mb_recording_id")


class EnrichmentQueueRepo(Repo):
    @synchronized
    def mark_recordings_enriched(self, recording_ids, now):
        self.conn.executemany('UPDATE recordings SET first_enriched_at=COALESCE(first_enriched_at,?),last_enriched_at=? WHERE id=?',
                              [(now,now,rid) for rid in recording_ids])
        self.conn.commit()

    # Track plays and the play sums of each track's playlist, album and artist containers.
    _QUEUE_CTES = """
        WITH plays AS (SELECT CAST(identity_key AS TEXT) identity_key, COUNT(*) c FROM listening_days GROUP BY identity_key),
        tp AS MATERIALIZED (SELECT t.id,t.recording_id, COALESCE(p.c,0) sp FROM catalog_tracks t
               LEFT JOIN plays p ON p.identity_key=t.identity_key),
        pl_plays AS (SELECT playlist_id,SUM(sp) c FROM (
                     SELECT DISTINCT pt.playlist_id,tp.recording_id,tp.sp FROM playlist_tracks pt
                     JOIN tp ON tp.id=pt.track_id) GROUP BY playlist_id),
        al_plays AS (SELECT album_id,SUM(sp) c FROM (
                     SELECT DISTINCT at.album_id,tp.recording_id,tp.sp FROM {album_tracks} at
                     JOIN tp ON tp.id=at.entry_id) GROUP BY album_id),
        ar_plays AS (SELECT ar,SUM(sp) c FROM (
                     SELECT DISTINCT t.artist ar,tp.recording_id,tp.sp FROM catalog_tracks t
                     JOIN tp ON tp.id=t.id WHERE t.artist IS NOT NULL AND t.artist<>'') GROUP BY ar),
        recording_album_plays AS MATERIALIZED (
            SELECT e.recording_id,MAX(ap.c) c FROM al_plays ap
            JOIN {album_tracks} at ON at.album_id=ap.album_id
            JOIN catalog_entries e ON e.id=at.entry_id GROUP BY e.recording_id)
    """

    @synchronized
    def priority_candidates(self, keys, limit, stale_before, *, providers, now, eligibility):
        rows = self.conn.execute(
            "WITH " + TRACKS_CTE + " SELECT * FROM enrichment_tracks t "
            "WHERE identity_key IN (SELECT value FROM json_each(:keys)) "
            "AND " + eligibility +
            " ORDER BY (SELECT CAST(key AS INTEGER) FROM json_each(:keys) "
            "WHERE value=t.identity_key), queue_order LIMIT :limit",
            {"keys": json.dumps(keys), "limit": limit,
             "stale_before": stale_before, "providers": json.dumps(providers) if providers is not None else None,
             "now": now}).fetchall()
        return [dict(row) for row in rows]

    @synchronized
    def provider_configuration(self, providers):
        return [tuple(row) for row in self.conn.execute(
            "SELECT provider,fingerprint,status FROM enrichment_provider_config "
            "WHERE :providers IS NULL OR provider IN (SELECT value FROM json_each(:providers)) "
            "ORDER BY provider", {"providers": json.dumps(providers) if providers is not None else None})]

    @synchronized
    def admit_candidates(self, candidates, configuration, *, providers, now, eligibility, stale_before=None):
        # A changed configuration can replace the same recording/provider job. Defer it.
        if self.provider_configuration(providers) != configuration:
            return []
        rows = self.conn.execute(
            "WITH " + TRACKS_CTE + " SELECT t.* FROM json_each(:candidates) selected "
            # Constrain the view's outer recording PK to avoid scanning all subjects per input.
            "JOIN enrichment_tracks t ON t.queue_order=json_extract(selected.value,'$.recording_id') "
            "AND t.recording_id=t.queue_order "
            "AND t.id IS json_extract(selected.value,'$.id') "
            "AND t.last_enriched_at IS json_extract(selected.value,'$.last_enriched_at') "
            "WHERE " + eligibility + " ORDER BY CAST(selected.key AS INTEGER)",
            {"candidates": json.dumps([{key: track.get(key) for key in
                                        ("recording_id", "id", "last_enriched_at")} for track in candidates]),
             "providers": json.dumps(providers) if providers is not None else None,
             "now": now, "stale_before": stale_before}).fetchall()
        return [dict(row) for row in rows]

    @synchronized
    def priority_order(self, *, caught_up, ordering):
        # Completion and provider retry state do not affect this catalog order.
        with column_group_reads(self._store, "recordings", "catalog"):
            cursor = self.conn.execute(self._QUEUE_CTES.format(album_tracks=self.album_tracks_table) + ", " + TRACKS_CTE + """,
          queue AS (
            SELECT t.queue_order,t.created_at,t.source,COALESCE(p.c,0) sp,
              MAX(COALESCE((SELECT MAX(plp.c) FROM playlist_tracks pt JOIN pl_plays plp
                            ON plp.playlist_id=pt.playlist_id WHERE pt.track_id=t.id), 0),
                  COALESCE((SELECT c FROM recording_album_plays WHERE recording_id=t.recording_id), 0),
                  COALESCE((SELECT c FROM ar_plays WHERE ar=t.artist), 0)) AS best
            FROM enrichment_tracks t LEFT JOIN plays p ON p.identity_key=t.identity_key
          )
            SELECT queue_order FROM queue
            ORDER BY """ + ordering, {"caught_up": caught_up})
            try:
                order = []
                while rows := cursor.fetchmany(512):
                    check_cancellable_work()
                    order.extend(row[0] for row in rows)
            finally:
                cursor.close()
        check_cancellable_work()
        # Reuse encoded IDs; SQL seeks current subjects without hydrating the library.
        return json.dumps(order, separators=(",", ":"))

    @synchronized
    def has_candidates(self, *, providers, now, eligibility):
        return self.conn.execute("WITH " + TRACKS_CTE + " SELECT 1 FROM enrichment_tracks t WHERE " +
            eligibility + " LIMIT 1", {"providers": json.dumps(providers) if providers is not None else None,
                                      "now": now}).fetchone() is not None

    @synchronized
    def select_candidates(self, limit, offset, *, priorities, providers, now, eligibility):
        params = {"limit": limit, "offset": offset, "priorities": priorities,
                  "providers": json.dumps(providers) if providers is not None else None,
                  "now": now}
        rows = self.conn.execute("WITH " + TRACKS_CTE + """, chosen AS MATERIALIZED (
            SELECT t.queue_order,CAST(priority.key AS INTEGER) AS priority,
              COALESCE((SELECT MAX(s.at) FROM enrichment_provider_state s
                WHERE s.recording_id=t.recording_id AND s.cycle=COALESCE(t.last_enriched_at,-1)),0) AS attempted
            FROM json_each(:priorities) priority CROSS JOIN enrichment_tracks t
              ON t.queue_order=priority.value AND t.recording_id=t.queue_order
            WHERE """ + eligibility + """
            ORDER BY attempted,CAST(priority.key AS INTEGER) LIMIT :limit OFFSET :offset
          ) SELECT """ + ",".join("t." + field for field in _QUEUE_FIELDS) + """
            FROM chosen CROSS JOIN enrichment_tracks t
              ON t.queue_order=chosen.queue_order AND t.recording_id=t.queue_order
            ORDER BY chosen.attempted,chosen.priority""", params).fetchall()
        return [dict(row) for row in rows]

    @synchronized
    def resweep_candidates(self, limit, stale_before, *, providers, now, eligibility):
        rows = self.conn.execute(
            "WITH " + TRACKS_CTE + " SELECT * FROM enrichment_tracks t "
            "WHERE " + eligibility +
            " ORDER BY COALESCE((SELECT MAX(s.at) FROM enrichment_provider_state s "
            "WHERE s.recording_id=t.recording_id AND s.cycle=COALESCE(t.last_enriched_at,-1)),0), "
            "last_enriched_at ASC, queue_order ASC, source ASC LIMIT :limit",
            {"stale_before": stale_before, "limit": limit,
             "providers": json.dumps(providers) if providers is not None else None,
             "now": now}).fetchall()
        return [dict(row) for row in rows]

    @synchronized
    def requeue_enrichment(self, track_ids) -> None:
        """Fresh user intent retries incomplete tracks before the slow stale sweep."""
        track_ids = list(dict.fromkeys(catalog.resolve_entry(self.conn,t) for t in track_ids))
        if not track_ids:
            return
        qs = ",".join("?" * len(track_ids))
        self.conn.execute(
            f"UPDATE recordings SET first_enriched_at=NULL, last_enriched_at=NULL "
            f"WHERE id IN (SELECT recording_id FROM catalog_entries WHERE id IN ({qs}))",
            track_ids)
        self.conn.commit()
