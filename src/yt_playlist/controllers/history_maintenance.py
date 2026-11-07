"""Ordered removal and verified repair over independent persistence owners."""
import hashlib
import json
import logging
import math

from yt_playlist.core.repair import PHANTOM_PURGE_SEEN, CORROBORATION_TOLERANCE_DAYS

logger = logging.getLogger(__name__)


class HistoryMaintenance:
    def __init__(self, repository, settings, discovery, vectors, selection, proposals, files, guard):
        self.repository, self.settings, self.discovery = repository, settings, discovery
        self.vectors, self.selection, self.proposals = vectors, selection, proposals
        self.files, self.guard = files, guard

    def repair_verified_imports(self):
        with self.guard():
            imports = self.repository.verified_imports()
            if not imports:
                return
            # Hash every verified interval: an older import can be verified later.
            evidence = [tuple(row.values()) for row in imports]
            watermark = hashlib.sha256(json.dumps(evidence).encode()).hexdigest()
            if watermark == self.settings.get_setting(PHANTOM_PURGE_SEEN):
                return
            windows = []
            for row in imports:
                start, end = row['coverage_start'], row['coverage_end']
                if not math.isfinite(start) or not math.isfinite(end):
                    continue
                lo, hi = math.ceil(start / 86400), math.floor(end / 86400) - 1
                if lo <= hi:
                    windows.append((row['account_id'], lo, hi))
            try:
                rows = self.repository.contradicted_observations(windows, CORROBORATION_TOLERANCE_DAYS)
                if rows:
                    path = self.files.publish(rows, watermark)
                    self.repository.delete_observations([row['id'] for row in rows])
                    logger.info('repair: purged %d contradicted YouTube observations; recoverable from %s',
                                len(rows), path)
            except Exception:
                logger.warning('repair skipped; history preserved, no successful purge recorded', exc_info=True)
                return
            self.settings.set_setting(PHANTOM_PURGE_SEEN, watermark)

    def remove_spotify_history(self):
        with self.guard():
            before = self.repository.spotify_history_stats()
            # Invalidate learned outputs and restore points first. If a later
            # owner fails, retry repeats these idempotent clears before removal.
            self.proposals.discard_learned_outputs()
            self.selection.discard_history_models()
            self.vectors.discard_history_models()
            for key in ('rec_artist_content_model', 'rec_fit_inputs', 'rec_artist_fit_inputs',
                        'rec_mode_fit_inputs', 'rec_refresh_version', 'rec_autotune_result',
                        'trend_spotlight_review_month', 'rec_selection_active_run',
                        'rec_selection_display_run', 'rec_selection_live_run', 'rec_selection_restore'):
                self.settings.delete_setting(key)
            artists_removed = self.discovery.remove_spotify_origins()
            self.repository.delete_spotify_receipts()
            for source in ('spotify_export', 'spotify'):
                self.repository.remove_listening_source(source)
            for key in ('spotify_imported_at', 'spotify_import_report'):
                self.settings.delete_setting(key)
            return {**before, 'artists_removed': artists_removed}
