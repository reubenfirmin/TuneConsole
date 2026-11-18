"""Serialized job dispatch over feature commands and scheduler-owned retry state."""
from yt_playlist.core.activity import WorkClosing
from yt_playlist.rec.worker_queue import Job


class PendingDispatch:
    def __init__(self, jobs, commands, *, stop, debounce, lock, model_lock, flush,
                 schedule, active, running, clock, logger):
        self.jobs, self.commands = jobs, commands
        self.stop, self.debounce, self.lock, self.model_lock = stop, debounce, lock, model_lock
        self.flush, self.schedule = flush, schedule
        self.active, self.running, self.clock, self.logger = active, running, clock, logger

    def run(self):
        while not self.stop.wait(self.debounce):
            if not self.commands.retirement_pending():
                self.flush()
            with self.lock:
                if not self.jobs:
                    self.running(False)
                    return
                batch = self.jobs.drain()
                if not batch and self.jobs.measurements and self.clock() < self.jobs.retry[Job.MEASUREMENT].retry_at:
                    self.running(False)
                    return
                self.active(any(job not in {Job.MEASUREMENT, Job.EVIDENCE_STORAGE, Job.RETENTION,
                    Job.CHOICES, Job.ANALYTICS, Job.GROUPS, Job.TV, Job.MODEL_HEALTH} for job in batch))
            full, modes = Job.FULL in batch, Job.MODES in batch
            try:
                self.commands.cleanup(batch)
            except WorkClosing:
                raise
            except Exception:
                self.commands.cleanup_failed()
                with self.lock:
                    for job, payload in batch.items():
                        if job != Job.RETENTION:
                            self.jobs.merge(job, payload)
                    self.jobs.pending.pop(Job.RETENTION, None)
                    self.jobs.retry[Job.RETENTION].failed(self.clock(), base=5, cap=300)
                    self.active(False)
                    self.running(False)
                self.logger.warning('Recommendation cleanup will retry before preparation', exc_info=True)
                return
            if Job.EVIDENCE_STORAGE in batch:
                state = self.jobs.retry[Job.EVIDENCE_STORAGE]
                state.retry_at = self.clock() + 300
                try:
                    result = self.commands.evidence()
                    if result and result.get('deferred'):
                        state.retry_at = self.clock() + 5
                except WorkClosing:
                    raise
                except Exception:
                    state.retry_at = self.clock() + 5
                    self.logger.warning('Listening evidence maintenance failed', exc_info=True)
            if Job.RETENTION in batch:
                state = self.jobs.retry[Job.RETENTION]
                state.retry_at = float('inf')
                state.retry_at = self.commands.retention()
            if self.commands.playback():
                self.schedule(full=False, ratings=True)
            if full and self.clock() < self.jobs.retry[Job.FULL].retry_at:
                self.jobs.retry[Job.FULL].dirty = True
                full = False
            keys = batch.get(Job.CONTENT, set())
            if Job.RATINGS in batch:
                try:
                    keys.update(self.commands.ratings())
                except WorkClosing:
                    raise
                except Exception:
                    self.logger.warning('rating subject update failed', exc_info=True)
            preparation_refresh = False
            try:
                if Job.PREPARE in batch:
                    preparation_refresh = self.commands.prepare()
                    if preparation_refresh and Job.BUNDLES not in batch:
                        self.schedule(full=False, bundles=True)
                        preparation_refresh = False
                if full and not self.commands.full_due():
                    self.jobs.retry[Job.FULL].dirty = False
                    full = False
                if full:
                    self.commands.rebuild()
                elif modes:
                    self.commands.modes()
                if Job.BUNDLES in batch:
                    with self.model_lock:
                        self.commands.bundles()
                    preparation_refresh = False
            except WorkClosing:
                raise
            except Exception:
                if preparation_refresh:
                    self.schedule(full=False, bundles=True)
                backoff = self.jobs.retry[Job.FULL].failed(self.clock(), base=2, exponent_cap=4)
                self.logger.warning('rec worker rebuild failed: retry_backoff=%.1fs', backoff, exc_info=True)
                if full and modes:
                    self.schedule(full=False, modes=True)
            for job, name in ((Job.TRENDS, 'trends'), (Job.CLEANUP, 'cleanup'),
                              (Job.ANALYTICS, 'analytics'), (Job.MODEL_HEALTH, 'model_health')):
                if job in batch:
                    with self.model_lock:
                        self.commands.projection(name)
            if keys:
                try:
                    self.commands.content_changed()
                    self.schedule(full=True, prepare=True)
                except WorkClosing:
                    raise
                except Exception:
                    self.logger.warning('rec content refresh failed: tracks=%d', len(keys), exc_info=True)
            if Job.HOME in batch:
                try:
                    state = self.jobs.retry[Job.HOME]
                    state.attempted_at = self.clock()
                    with self.model_lock:
                        dirty = self.commands.home()
                    with self.lock:
                        state.dirty |= dirty
                    self.commands.poll_tv()
                except WorkClosing:
                    raise
                except Exception:
                    with self.lock:
                        self.jobs.retry[Job.HOME].dirty = True
                    self.logger.warning('Home preparation failed; retaining previous collections', exc_info=True)
            if Job.CHOICES in batch:
                state = self.jobs.retry[Job.CHOICES]
                state.started(self.clock())
                try:
                    with self.model_lock:
                        if self.commands.choices():
                            self.schedule(full=False, bundles=True, home=True)
                    state.succeeded()
                except WorkClosing:
                    raise
                except Exception:
                    state.failed(self.clock(), base=60, cap=3600)
                    self.logger.warning('Daily listening evaluation failed; retaining previous report', exc_info=True)
            if Job.ARTWORK in batch:
                try:
                    self.commands.artwork(batch[Job.ARTWORK])
                except WorkClosing:
                    raise
                except Exception:
                    self.logger.warning('Artist image repair failed', exc_info=True)
            if Job.STORIES in batch:
                try:
                    self.jobs.retry[Job.STORIES].attempted_at = self.clock()
                    if self.commands.stories():
                        self.schedule(full=False, stories=True)
                except WorkClosing:
                    raise
                except Exception:
                    self.logger.warning('Story preparation failed; retaining previous stories', exc_info=True)
            if Job.TV in batch:
                state = self.jobs.retry[Job.TV]
                if self.clock() >= state.retry_at:
                    state.started(self.clock())
                    try:
                        more, needs_fill = self.commands.tv()
                        state.succeeded()
                        state.dirty = more
                        if more and needs_fill:
                            state.retry_at = min(state.retry_at, state.attempted_at + 5)
                    except WorkClosing:
                        raise
                    except Exception:
                        state.failed(self.clock(), base=60, cap=3600)
                        self.logger.warning('TV preparation failed; retaining previous videos', exc_info=True)
            if Job.GROUPS in batch:
                self.commands.groups()
            self.active(False)
        self.running(False)
