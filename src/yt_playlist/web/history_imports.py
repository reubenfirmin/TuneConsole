"""Tracked local history processing, serialized with library history refreshes."""
import threading
import json
from contextlib import nullcontext

from yt_playlist.core.activity import WorkClosing, activity_for
from yt_playlist.library import spotify_data, takeout
from yt_playlist.web.jobs import SyncJobs


class HistoryImports:
    def __init__(self, ctx):
        self.ctx = ctx
        # Small route-only contexts use the same ownership rules as the full application.
        if not hasattr(ctx, 'jobs'):
            ctx.jobs = SyncJobs()
        if not hasattr(ctx, 'sync_lock'):
            ctx.sync_lock = threading.Lock()

    async def run(self, source, raw):
        job = self.ctx.jobs.create(kind='history_import')
        job.parameters = {'source': source}
        job.events.append({'text': 'Waiting for other history work…'})
        try:
            return await activity_for(self.ctx).to_thread(self._run, job, source, raw)
        except WorkClosing:
            job.error = 'TuneConsole is closing. Try the import again after reopening.'
            job.outcome, job.done = 'failed', True
            raise

    def _run(self, job, source, raw):
        store = self.ctx.store
        progress = lambda text: job.events.append({'text': text})
        try:
            with self.ctx.sync_lock, getattr(self.ctx.rec_worker, 'model_lock', nullcontext()):
                importer = takeout.import_takeout if source == 'takeout' else spotify_data.import_spotify_data
                report = importer(store, raw, on_progress=progress)
                if 'error' in report:
                    job.error = report['error']
                    job.outcome = 'failed'
                    job.result = {'report': report, 'seeded': 0}
                    return job.result
                if report['matched']:
                    store.settings.set_setting(source + '_imported_at', str(self.ctx.now_fn()))
                    if source == 'takeout':
                        progress('Checking imported history…')
                        from yt_playlist.core import repair
                        repair.run_once(store)
                elif source == 'takeout':
                    store.settings.set_setting('takeout_nag_dismissed_at', str(self.ctx.now_fn()))
                progress('Adding unmatched artists to Discovery…')
                min_plays = max(3, round(report['span_days'] / 365))
                if source == 'spotify':
                    identity_id = report.get('identity_id')
                    if identity_id is None:
                        identity_id = spotify_data.resolve_import_identity(store)
                    seeded = store.discovery.seed_spotify_artists(
                        identity_id,
                        report['unmatched_artists'], self.ctx.now_fn(), min_plays=min_plays)
                    # Persist only aggregate reporting, never the uploaded file or its metadata.
                    summary = {k: v for k, v in report.items() if isinstance(v, (int, float))}
                    store.settings.set_setting('spotify_import_report', json.dumps(summary))
                else:
                    seeded = takeout.seed_discovery_from_unmatched(
                        store, report['unmatched_artists'], self.ctx.now_fn(), min_plays=min_plays)
                if (report['plays_added'] or report['events_added'] or report.get('listens_added') or seeded) and self.ctx.rec_worker:
                    self.ctx.rec_worker.trigger()
                if source == 'spotify':
                    report = {k: v for k, v in report.items() if k != 'unmatched_artists'}
                job.result = {'report': report, 'seeded': seeded}
                job.outcome = 'complete'
                progress('Import complete.')
                return job.result
        except Exception as exc:
            job.error = str(exc) or type(exc).__name__
            job.outcome = 'failed'
            progress('Import failed: ' + job.error)
            raise
        finally:
            job.done = True

    async def remove_spotify(self):
        job = self.ctx.jobs.create(kind='history_import')
        job.parameters = {'source': 'spotify'}
        job.events.append({'text': 'Waiting for history and model work…'})
        try:
            return await activity_for(self.ctx).to_thread(self._remove_spotify, job)
        except WorkClosing:
            job.error = 'TuneConsole is closing. Try removal again after reopening.'
            job.outcome, job.done = 'failed', True
            raise

    def _remove_spotify(self, job):
        worker = self.ctx.rec_worker
        try:
            with self.ctx.sync_lock, getattr(worker, 'model_lock', nullcontext()):
                report = self.ctx.store.history.remove_spotify_history()
                if worker:
                    worker.trigger()
                job.result = report
                job.outcome = 'complete'
                job.events.append({'text': 'Spotify history removed. Model refresh requested.'})
                return report
        except Exception as exc:
            job.error = str(exc) or type(exc).__name__
            job.outcome = 'failed'
            job.events.append({'text': 'Removal failed: ' + job.error})
            raise
        finally:
            job.done = True

    def status(self, source):
        job = self.ctx.jobs.latest('history_import', {'source': source})
        if job is None:
            return {'job_id': None, 'done': True, 'outcome': None, 'error': None, 'message': ''}
        return {'job_id': job.id, 'done': job.done, 'outcome': job.outcome, 'error': job.error,
                'message': job.events[-1]['text'] if job.events else ''}
