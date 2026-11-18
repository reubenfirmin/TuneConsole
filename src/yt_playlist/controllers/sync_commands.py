"""Serialized manual and recurring synchronization, independent of HTTP and hosts."""
from yt_playlist.core.activity import WorkClosing


def recent_result(result):
    events = []
    for account in result.get('accounts', {}).values():
        resources = account.get('resources', {})
        titles = {p['playlistId']: p.get('title', p['playlistId'])
                  for p in resources.get('playlist_listing', {}).get('entries', [])}
        for name, resource in resources.items():
            events.append({'type': 'info', 'text': f"{account.get('label', '')}: "
                f"{name.replace('_', ' ')} — {resource['status']}. {resource.get('message', '')}"})
            for key, item in resource.get('items', {}).items():
                events.append({'type': 'info' if item['status'] == 'complete' else 'err',
                    'text': f"{titles.get(key, key)}: {item['status']}. {item.get('message', '')}"})
    for name, resource in result.get('resources', {}).items():
        events.append({'type': 'info', 'text': f"{name.replace('_', ' ')} — "
            f"{resource['status']}. {resource.get('message', '')}"})
    return {'events': events, 'outcome': result.get('status')}


class SyncCommands:
    def __init__(self, jobs, lock, work, execution, *, due, state, enabled):
        self.jobs, self.lock, self.work, self.execution = jobs, lock, work, execution
        self.due, self.state, self.enabled = due, state, enabled
        self.last_connection = None

    def run(self, job, **options):
        try:
            outcome = self.execution().run(job.events.append, **options)
            job.result, job.outcome, job.error = outcome.result, outcome.outcome, outcome.error
        except WorkClosing:
            # The attempt's durable checkpoints remain available for resuming.
            # Only this job ends; shutdown cancellation is not a worker failure.
            job.outcome = 'cancelled'
        finally:
            job.done = True

    def start(self):
        job = self.jobs.create(kind='library')
        def run():
            with self.lock:
                self.run(job)
        try:
            self.work.start_thread(run)
        except BaseException:
            job.outcome, job.done = 'failed', True
            raise
        return job

    def tick(self, *, configured, connected, connection, max_age_s):
        release = self.work.admit('automatic sync')
        try:
            if not self.enabled() or not configured or not connected:
                return
            retry_now = connection is not None and connection != self.last_connection
            if not self.due(max_age_s, retry_now=retry_now):
                self.last_connection = connection
                return
            if not self.lock.acquire(blocking=False):
                return
            try:
                job = self.jobs.create(kind='library')
                with self.work.entered(release):
                    self.last_connection = connection
                    self.run(job, automatic=True, retry_now=retry_now, max_age_s=max_age_s)
                return job
            finally:
                self.lock.release()
        finally:
            release()

    def current(self):
        return self.jobs.find_active_library()

    def recent(self):
        job = self.jobs.latest('library', None)
        if job:
            return {'events': [event for _, event in job.events.since(0)[1]], 'outcome': job.outcome}
        return recent_result(self.state())
