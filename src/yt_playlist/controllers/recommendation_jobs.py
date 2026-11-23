"""Feature commands invoked by the serialized recommendation scheduler."""
from dataclasses import dataclass

from yt_playlist.core.activity import WorkClosing, WorkBudgetExceeded, cancellable_work


class PendingCommands:
    def __init__(self, store, now, stage, tasks, *, retention, prepare, full_due,
                 rebuild, modes, bundles, projection, poll_tv, groups):
        self.store, self.now, self.stage, self.tasks = store, now, stage, tasks
        self.retention, self.prepare, self.full_due = retention, prepare, full_due
        self.rebuild, self.modes, self.bundles = rebuild, modes, bundles
        self.projection, self.poll_tv, self.groups = projection, poll_tv, groups

    def retirement_pending(self):
        return getattr(self.store, '_recommendation_retirement_pending', False)

    def cleanup(self, batch):
        from yt_playlist.rec import candidate_refresh, recommendation_retirement
        from yt_playlist.rec.worker_queue import Job
        pending = self.retirement_pending()
        if Job.RETENTION in batch:
            pending |= self.store.recommendations.retirement_pending()
        if pending or (any(job in batch for job in (Job.FULL, Job.MODES, Job.BUNDLES))
                       and candidate_refresh.check_due(self.store, self.now())):
            self.stage('recommendation cleanup', recommendation_retirement.drain, self.store, self.now())

    def cleanup_failed(self):
        self.store._recommendation_retirement_pending = True

    def evidence(self):
        from yt_playlist.rec import choice_evidence
        return self.stage('listening evidence storage', choice_evidence.maintain_storage, self.store, self.now())

    def playback(self):
        from yt_playlist.core import playback_feedback
        return playback_feedback.try_drain(self.store)

    def ratings(self):
        return self.tasks.ratings()

    def content_changed(self):
        self.store.rec_refresh.enqueue()

    def home(self):
        return self.tasks.home()

    def choices(self):
        before = self.store.settings.get_setting('rec_selection_active_run')
        result = self.tasks.choices()
        after = self.store.settings.get_setting('rec_selection_active_run')
        return before != after or bool(result and result.get('published_vectors'))

    def artwork(self, payload):
        return self.tasks.artwork(payload)

    def stories(self):
        return self.tasks.stories()

    def tv(self):
        from yt_playlist.rec import tv
        more = self.tasks.tv()
        return more, bool(more and len(tv.queue(self.store, self.now())) < tv.QUEUE_SIZE)


class ModelCommands:
    def __init__(self, store, now, stage, preparation, fit_cache, stopping, logger):
        self.store, self.now, self.stage = store, now, stage
        self.preparation, self.fit_cache, self.stopping = preparation, fit_cache, stopping
        self.logger = logger

    def fit(self):
        from yt_playlist.rec import model_refresh, model_selection
        restore = self.store.settings.get_setting('rec_selection_restore')
        restored = False
        if restore:
            self.stage('model followup', model_selection.update_followup, self.store,
                       self.now(), now_fn=self.now)
            restored = self.store.model_selection.restore_model_selection(int(restore), self.now())
            self.store.settings.delete_setting('rec_selection_restore')
        checkpoint = self.preparation.full_checkpoint(self.now())

        def calculate():
            try:
                with cancellable_work(self.stopping):
                    return model_refresh.rebuild(self.store, self.now(), keep_collaborative=restored,
                        memory_bytes=self.preparation.budget.full_memory_bytes, fit_cache=self.fit_cache)
            except model_refresh.MemoryBudgetExceeded:
                self.store.maintenance_diagnostics.record('deferred', reasons=('full-fit-memory-budget',))
            except WorkBudgetExceeded:
                self.store.maintenance_diagnostics.record('deferred', reasons=('full-fit-time-budget',))

        result = self.stage('recommendation models', calculate)
        if result is None:
            return None
        return result, checkpoint

    def complete(self, checkpoint):
        from yt_playlist.rec import model_selection
        self.preparation.completed_full(checkpoint)
        try:
            self.stage('model selection', model_selection.maintain, self.store, self.now(), now_fn=self.now)
        except WorkClosing:
            raise
        except Exception:
            self.logger.warning('automatic model comparison failed; retaining published models', exc_info=True)


@dataclass(frozen=True)
class GroupOutcome:
    state: str
    refresh_home: bool = False


class GroupCommands:
    def __init__(self, store, now, stage, preparation, stopping, logger):
        self.store, self.now, self.stage = store, now, stage
        self.preparation, self.stopping, self.logger = preparation, stopping, logger

    def _report(self, phase, reason):
        from yt_playlist.rec import group_preparation
        try:
            group_preparation.report(self.store, phase, self.now(), reason=reason)
        except WorkClosing:
            raise
        except Exception:
            self.logger.warning('Could not persist adaptive grouping %s progress; retry remains pending',
                                phase, exc_info=True)
            return False
        return True

    def prepare(self):
        from yt_playlist.rec import group_preparation, group_serving, group_viz
        try:
            if self.store.settings.get_setting('rec_group_shadow') == '0':
                self.store.maintenance_diagnostics.resolve_wait(group_serving.PHASE, 'disabled')
                return GroupOutcome('ready')

            def calculate():
                with cancellable_work(self.stopping):
                    self.preparation.step(self.now())
                    if self.store.taste_groups.head_generation() is None:
                        self.store.maintenance_diagnostics.record(
                            'deferred', phase=group_serving.PHASE, reasons=('group-head',))
                        return None
                    group_viz.prepared(self.store)
                    now = self.now()
                    return group_serving.prepare(self.store, now) if group_serving.due(self.store, now) else {}

            preview = self.stage('adaptive grouping shadow', calculate)
            progress = group_preparation.status(self.store)
            phase = progress.get('phase')
            if preview is None:
                if phase == 'ready':
                    group_preparation.report(self.store, 'deferred', self.now(), reason='preview-pending')
                return GroupOutcome('deferred')
            preview_wait = phase == 'deferred' and progress.get('reason') in {'preview-pending', 'serving-work'}
            if phase in {'failed', 'preparing'} or (preview_wait and not self.preparation.due(self.now())):
                group_preparation.report(self.store, 'ready', self.now())
            return GroupOutcome('ready', bool(preview and group_serving.enabled(self.store)))
        except WorkClosing:
            raise
        except WorkBudgetExceeded:
            self.store.maintenance_diagnostics.record('deferred', phase=group_serving.PHASE,
                                                       reasons=('serving-work',))
            return GroupOutcome('deferred' if self._report('deferred', 'serving-work') else 'failed')
        except Exception:
            self.logger.warning('Adaptive grouping deferred; retaining previous shadow', exc_info=True)
            self._report('failed', 'preparation-failed')
            return GroupOutcome('failed')


class MaintenanceCommands:
    def __init__(self, store, now, preparation):
        self.store, self.now, self.preparation = store, now, preparation

    def groups_due(self, dirty, *, admitted=True):
        from yt_playlist.rec import group_preparation, group_serving
        if self.store.settings.get_setting('rec_group_shadow') == '0':
            self.store.maintenance_diagnostics.resolve_wait(group_serving.PHASE, 'disabled')
            return False
        if not admitted:
            return False
        due = (dirty or self.preparation.groups.due(self.now())
               or group_preparation.status(self.store).get('phase') in {'failed', 'preparing', 'deferred'})
        if not due and (generation := self.store.taste_groups.head_generation()):
            preview = self.store.rec_proposals.get_proposals(group_serving.SURFACE)
            due = (not preview or preview['generation'] != generation
                   or preview.get('schema') != group_serving.SCHEMA
                   or group_serving.due(self.store, self.now()))
        return due

    def full_due(self, dirty=False):
        return ((dirty or self.store.rec_refresh.has_pending_changes())
                and self.preparation.full_due(self.now()))

    def bundles_due(self):
        from yt_playlist.rec import candidate_refresh
        now = self.now()
        if candidate_refresh.check_due(self.store, now):
            if (not self.store.recommendations.head('home')
                    or candidate_refresh.due(self.store, now)):
                return True
            candidate_refresh.checked(self.store, now)
        return False

    def choices_due(self):
        from yt_playlist.rec import choice_evidence, daily_model_selection
        return (choice_evidence.daily_due(self.store, self.now())
                or daily_model_selection.due(self.store, self.now()))

    def tv_due(self, dirty):
        from yt_playlist.rec import tv
        return dirty or tv.due(self.store, self.now())


class CollectionCommands:
    def __init__(self, store, clients, now, execute, logger, discovery):
        self.store, self.clients, self.now, self.execute = store, clients, now, execute
        self.logger, self.discovery = logger, discovery

    def next_retry(self):
        from yt_playlist.library import playlist_gc_retry
        return playlist_gc_retry.next_retry_at(self.store)

    def generated(self, full):
        from yt_playlist.library import playlist_generated
        retry = self.next_retry()
        due = full or retry is not None and retry <= self.now()
        clients = (self.clients() or {}) if due else {}
        if clients:
            options = {} if full else {'retry_only': True}
            collected = self.execute('generated-playlist collection',
                playlist_generated.gc_generated_playlists, self.store, clients, self.now(), **options)()
            if collected:
                self.logger.info('GC: collected %d unplayed generated playlist(s): %s',
                                 len(collected), ', '.join(item['title'] for item in collected))

    def recovery(self):
        self.execute('collection recovery cleanup', self.store.retention.maintain_collection_recovery)()

    def recovery_pending(self):
        return self.store.retention.collection_recovery_pending()

    def discoveries(self):
        result = self.execute('discovery collection', self.discovery, self.now())()
        if any(result.values()):
            self.logger.info('GC: discovery pool removed %s', result)
