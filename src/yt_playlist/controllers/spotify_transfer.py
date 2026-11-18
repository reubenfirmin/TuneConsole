"""Single-owner Spotify previews, transfer jobs and durable report operations."""
from contextlib import contextmanager

from yt_playlist.library import spotify_transfer_policy as policy
from yt_playlist.core.spotify_job_state import TOKEN


class TransferRejected(ValueError):
    def __init__(self, reason, detail):
        self.reason, self.detail = reason, detail
        super().__init__(detail)


class TransferReports:
    def __init__(self, settings, lock, *, now, publish_recovery):
        self.settings, self.lock, self.now = settings, lock, now
        self.publish_recovery = publish_recovery

    def load(self):
        try:
            value = self.settings.spotify_import_job()
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def save(self, job):
        revision = self.settings.save_spotify_import_job(policy.retained_report(job))
        if revision is not None:
            job[TOKEN] = revision

    def current(self):
        job = self.load()
        if not job or job.get('status') not in policy.ACTIVE_STATUSES:
            return job
        if not self.lock.acquire(blocking=False):
            return job
        try:
            job = self.load()
            if job and job.get('status') in policy.ACTIVE_STATUSES:
                job = policy.recovered(job, self.now())
                self.publish_recovery(job)
            return job
        finally:
            self.lock.release()

    def delete(self):
        self.settings.delete_setting(policy.JOB_KEY)


class SpotifyTransfer:
    def __init__(self, reports, previews, accounts, work, *, destination, parse, prepare,
                 transfer, candidates, save_album, imported, now, logger, report_schema):
        self.reports, self.previews, self.accounts, self.work = reports, previews, accounts, work
        self.destination, self.parse, self.prepare_input = destination, parse, prepare
        self.transfer, self.candidates, self.save_album, self.imported = transfer, candidates, save_album, imported
        self.now, self.logger, self.report_schema = now, logger, report_schema

    @contextmanager
    def operation(self):
        if not self.reports.lock.acquire(blocking=False):
            raise TransferRejected('busy', 'Wait for the current Spotify import or album operation to finish')
        try:
            yield
        finally:
            self.reports.lock.release()

    def snapshot(self, token, stage, *, consume=False):
        value = self.previews.get(token, self.now(), consume=consume)
        if value is None or value['stage'] != stage:
            raise TransferRejected('expired', 'Preview expired or already used. Upload the ZIP again.')
        return value

    def select(self, raw):
        parsed = self.parse(raw)
        token = self.previews.put({'stage': 'selection', 'parsed': parsed}, self.now())
        return {'parsed': parsed, 'identities': self.accounts(), 'selection_token': token}

    def _job(self, phase, identity, parsed):
        return {'status': 'running', 'phase': phase, 'started_at': self.now(),
                'account_id': identity.id, 'identity_label': identity.label,
                'report_schema': self.report_schema, **policy.estimate(parsed)}

    def _start(self, token, stage, job, target, name):
        if not self.reports.lock.acquire(blocking=False):
            raise TransferRejected('busy', 'Another Spotify import is running')
        def run():
            try:
                target(job)
            finally:
                self.reports.lock.release()
        try:
            self.snapshot(token, stage, consume=True)
            self.reports.save(job)
            self.work.start_thread(run, name=name)
        except BaseException:
            self.reports.lock.release()
            raise

    def prepare(self, token, account_id, indices, include_albums):
        snapshot = self.snapshot(token, 'selection')
        identity, client = self.destination(account_id)
        try:
            selected = policy.selection(snapshot['parsed'], indices, include_albums)
        except (ValueError, TypeError) as exc:
            raise TransferRejected('invalid', str(exc) or 'Invalid playlist selection') from exc
        def prepare(job):
            try:
                prepared = self.prepare_input(selected, client, identity.id)
                ready = self.previews.put({'stage': 'ready', 'prepared': prepared,
                                          'account_id': identity.id}, self.now())
                self.reports.save({**job, 'status': 'preview', 'preview_token': ready})
            except Exception:
                self.logger.exception('Spotify import preview failed')
                self.reports.save({**job, 'status': 'error',
                    'error': 'Could not prepare the preview. Upload the ZIP again.'})
        self._start(token, 'selection', self._job('preview', identity, selected), prepare, 'spotify-library-preview')
        return self.reports.current()

    def preview(self, token):
        snapshot = self.snapshot(token, 'ready')
        identity, _ = self.destination(snapshot['account_id'])
        return {'prepared': snapshot['prepared'], 'identity': identity, 'preview_token': token}

    def confirm(self, token, actions):
        snapshot = self.snapshot(token, 'ready')
        identity, client = self.destination(snapshot['account_id'])
        try:
            prepared = policy.album_actions(snapshot['prepared'], actions)
        except ValueError as exc:
            raise TransferRejected('invalid', str(exc)) from exc
        def run(job):
            try:
                report = self.transfer(prepared, client, identity.id, self.now())
                if report['tracks_imported'] or report['albums_imported']:
                    self.imported(self.now())
                self.reports.save({**job, 'status': 'done', 'finished_at': self.now(), 'report': report})
            except Exception:
                self.logger.exception('Spotify library import failed')
                self.reports.save({**job, 'status': 'error', 'finished_at': self.now(),
                    'error': 'The import stopped. Upload the same ZIP to resume recorded transfers.'})
        self._start(token, 'ready', self._job('import', identity, prepared), run, 'spotify-library-import')

    def cancel(self, token):
        with self.operation():
            self.snapshot(token, 'ready', consume=True)
            job = self.reports.current()
            if job and job.get('preview_token') == token:
                self.reports.delete()

    def report_album(self, job, index):
        if not job or job.get('status') != 'done':
            raise TransferRejected('missing', 'report not found')
        try:
            position = int(index)
            if position < 0:
                raise IndexError
            item = job['report']['details']['albums'][position]
        except (KeyError, IndexError, TypeError, ValueError):
            raise TransferRejected('missing', 'album outcome not found') from None
        if item.get('status') != 'unmatched':
            raise TransferRejected('resolved', 'album outcome is already resolved')
        return item

    def find(self, index):
        with self.operation():
            job = self.reports.current()
            item = self.report_album(job, index)
            try:
                item['candidates'] = self.candidates(self.destination(job.get('account_id'))[1], item)
                self.reports.save(job)
            except Exception:
                self.logger.exception('Spotify import candidate lookup failed')
                return 'YouTube Music could not search for candidates. Try again when connected.'

    def resolve(self, index, browse_id):
        with self.operation():
            job = self.reports.current()
            item = self.report_album(job, index)
            try:
                identity, client = self.destination(job.get('account_id'))
                outcome = self.save_album(client, item, browse_id, identity.id)
            except ValueError as exc:
                return str(exc)
            except Exception:
                self.logger.exception('Spotify import selected-album save failed')
                return 'YouTube Music could not save that album. Try again when connected.'
            report = job['report']
            item.update(outcome)
            item.pop('candidates', None)
            report['albums_unmatched'] = max(0, report['albums_unmatched'] - 1)
            report['albums_imported' if outcome['status'] == 'imported' else 'albums_skipped'] += 1
            self.reports.save({**job, 'report': report})
            if outcome['status'] == 'imported':
                self.imported(self.now())

    def dismiss(self):
        with self.operation():
            job = self.reports.current()
            if not job or job.get('status') not in policy.ACTIVE_STATUSES:
                self.reports.save({**job, 'dismissed': True}) if job else self.reports.delete()

    def delete(self):
        with self.operation():
            self.reports.delete()
