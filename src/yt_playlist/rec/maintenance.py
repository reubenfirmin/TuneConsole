"""Coalesced, restart-safe scheduling for disposable listening/library summaries."""
import threading
import time
from typing import Callable

from yt_playlist.core.activity import WorkClosing

# These are source tables, never recommendation checkpoints or player telemetry.
# Polling reads connection-local revision counters, not the library itself.
CATALOG = {'recordings', 'youtube_items', 'catalog_entries', 'library_memberships',
           'entry_field_choices', 'track_artists', 'artists', 'artist_redirects'}
PLAYLISTS = {'playlists', 'playlist_items', 'unresolved_playlist_items', 'playlist_scopes',
             'playlist_group', 'playlist_provenance', 'hidden_playlists', 'identities'}
SOURCES = {
    'trends': CATALOG | PLAYLISTS | {'play_events', 'play_event_calendar',
        'listening_observations', 'rec_feedback', 'album_tracks', 'albums',
        'album_library_memberships', 'album_provider_ids'},
    'cleanup': CATALOG | PLAYLISTS | {'cleanup_ignored', 'suppressed_overlaps',
        'overlap_ignored', 'overlap_kept', 'ignored_merges'},
}
SURFACES = {'trends': 'trend_rollups', 'cleanup': 'cleanup'}
DEPENDENCIES = {kind: {(table, '') for table in tables} for kind, tables in SOURCES.items()}
DEPENDENCIES['trends'].add(('settings', 'key:takeout_imported_at'))


def inputs(store, kind, now):
    return input_fingerprints(store, (kind,), now)[kind]


def input_fingerprints(store, kinds, now):
    """Capture just the summaries' dependencies once under the serving lock."""
    dependencies = set().union(*(DEPENDENCIES[kind] for kind in kinds))
    with store._lock:
        revisions = store._snapshot_revisions.capture(dependencies)
        # Importers can write through another connection. Conservatively refresh
        # on their commits; ordinary playback writes use this connection and only
        # relevant source changes invalidate these jobs.
        external = store.conn.execute('PRAGMA data_version').fetchone()[0]
        placeholders = ','.join('?' for _ in kinds)
        present = {row[0] for row in store.conn.execute(
            f'SELECT surface FROM rec_proposals WHERE surface IN ({placeholders})',
            tuple(SURFACES[kind] for kind in kinds))}
    return {kind: (
        tuple(sorted((table, revision) for (table, key), revision in revisions.items()
                     if (table, key) in DEPENDENCIES[kind])),
        external, int(now // 86400) if kind == 'trends' else None, SURFACES[kind] in present,
    ) for kind in kinds}


class Projections:
    def __init__(self, store, now_fn, logger, *, accepting: Callable[[], bool],
                 schedule: Callable[[dict[str, bool]], bool], execute: Callable):
        self.store, self.now_fn, self.logger = store, now_fn, logger
        self.accepting, self.schedule, self.execute = accepting, schedule, execute
        self.lock = threading.Lock()
        self.completed = {}
        self.active = set()
        self.retry_at = {}
        self.failures = {}

    def poll(self):
        if not self.accepting():
            return
        pending = {}
        for kind, fingerprint in input_fingerprints(self.store, SURFACES, self.now_fn()).items():
            with self.lock:
                if (kind not in self.active and fingerprint != self.completed.get(kind)
                        and time.monotonic() >= self.retry_at.get(kind, 0)):
                    self.active.add(kind)
                    pending[kind] = True
        if pending:
            if not self.schedule(pending):
                with self.lock:
                    self.active.difference_update(pending)

    def run(self, kind):
        from yt_playlist.rec import actions, trend_rollups
        result = None
        try:
            fingerprint = inputs(self.store, kind, self.now_fn())
            target = trend_rollups.build if kind == 'trends' else actions.refresh_cleanup
            result = self.execute(kind + ' summary', target, self.store, self.now_fn())
            return result
        except WorkClosing:
            raise
        except Exception:
            self.logger.warning('%s summary failed; retaining last-good result', kind, exc_info=True)
        finally:
            with self.lock:
                self.active.discard(kind)
                if result is None:
                    failures = self.failures.get(kind, 0) + 1
                    self.failures[kind] = failures
                    self.retry_at[kind] = time.monotonic() + min(300, 2 ** min(failures, 9))
                else:
                    # Publication creates the previously missing proposal. Source
                    # revisions remain those captured before calculation, so a
                    # concurrent edit cannot be marked clean accidentally.
                    self.completed[kind] = (*fingerprint[:-1], True)
                    self.failures[kind] = 0
                    self.retry_at[kind] = time.monotonic() + 30
