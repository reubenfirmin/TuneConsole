"""Tracked local history processing, serialized with library history refreshes."""
import json
import hashlib

from yt_playlist.core.activity import WorkClosing


class HistoryImports:
    def __init__(self, *, jobs, lock, model_lock, work, settings, history,
                 importers, seed_spotify, seed_takeout, resolve_spotify, refresh, now):
        self.jobs, self.lock, self.model_lock, self.work = jobs, lock, model_lock, work
        self.settings, self.history, self.importers = settings, history, importers
        self.seed_spotify, self.seed_takeout = seed_spotify, seed_takeout
        self.resolve_spotify, self.refresh, self.now = resolve_spotify, refresh, now

    async def run(self, source, raw):
        job = self.jobs.create(kind='history_import')
        job.parameters = {'source': source}
        job.events.append({'text': 'Waiting for other history work…'})
        try:
            return await self.work.to_thread(self._run, job, source, raw)
        except WorkClosing:
            job.error = 'TuneConsole is closing. Try the import again after reopening.'
            job.outcome, job.done = 'failed', True
            raise

    def _run(self, job, source, raw):
        progress = lambda text: job.events.append({'text': text})
        try:
            with self.lock, self.model_lock():
                report = self.importers[source](raw, on_progress=progress)
                if 'error' in report:
                    job.error = report['error']
                    job.outcome = 'failed'
                    job.result = {'report': report, 'seeded': 0}
                    return job.result
                if report['matched']:
                    self.settings.set_setting(source + '_imported_at', str(self.now()))
                    if source == 'takeout':
                        progress('Checking imported history…')
                        self.history.repair_verified_imports()
                elif source == 'takeout':
                    self.settings.set_setting('takeout_nag_dismissed_at', str(self.now()))
                progress('Adding unmatched artists to Discovery…')
                min_plays = max(3, round(report['span_days'] / 365))
                if source == 'spotify':
                    # The importer captures identity before parsing. Presence
                    # of that field is authoritative even if main changed.
                    account_id = report.get('identity_id', report.get('account_id'))
                    if 'identity_id' not in report and 'account_id' not in report:
                        account_id = self.resolve_spotify()
                    seeded = self.seed_spotify(
                        account_id,
                        report['unmatched_artists'], self.now(), min_plays=min_plays,
                        import_key=hashlib.sha256(raw.encode() if isinstance(raw,str) else raw).hexdigest())
                    # Persist only aggregate reporting, never the uploaded file or its metadata.
                    summary = {k: v for k, v in report.items() if isinstance(v, (int, float))}
                    self.settings.set_setting('spotify_import_report', json.dumps(summary))
                else:
                    seeded = self.seed_takeout(
                        report['unmatched_artists'], self.now(), min_plays=min_plays)
                if report['plays_added'] or report['events_added'] or report.get('listens_added') or seeded:
                    self.refresh()
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
        job = self.jobs.create(kind='history_import')
        job.parameters = {'source': 'spotify'}
        job.events.append({'text': 'Waiting for history and model work…'})
        try:
            return await self.work.to_thread(self._remove_spotify, job)
        except WorkClosing:
            job.error = 'TuneConsole is closing. Try removal again after reopening.'
            job.outcome, job.done = 'failed', True
            raise

    def _remove_spotify(self, job):
        try:
            with self.lock, self.model_lock():
                report = self.history.remove_spotify_history()
                self.refresh()
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
        job = self.jobs.latest('history_import', {'source': source})
        if job is None:
            return {'job_id': None, 'done': True, 'outcome': None, 'error': None, 'message': ''}
        return {'job_id': job.id, 'done': job.done, 'outcome': job.outcome, 'error': job.error,
                'message': job.events[-1]['text'] if job.events else ''}
