"""Decoupled recommendation worker (spec §3).

Rec computation runs OFF the sync/request path here: a single background thread rebuilds the
taste vectors and materializes the heavy/slow surfaces (fresh songs, outward discovery) into
rec_proposals for last-good serving. Triggers coalesce: many syncs in a row collapse into one
rebuild, so frequent syncs never pile up.
"""
import logging
import threading
import time
import traceback

from yt_playlist.rec import model_selection, measurement
from yt_playlist.core.activity import (activity_for, cancellable_work, check_stopping, WorkClosing,
                                       WorkBudgetExceeded)
from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.core.numerics import limit_native_threads
from yt_playlist.core.evidence_ledger import EvidenceBudgetExceeded
from yt_playlist.core.policy_config import PolicyCapacityFull
from yt_playlist.rec.worker_queue import Job, PendingJobs
from yt_playlist.rec.worker_tasks import ServingTasks

STORY_CHECK_INTERVAL_S = 30


class RecWorker:
    def __init__(self, ctx, debounce_s=2.0, discovery_tick_s=1800, gc_tick_s=86400, gc_initial_s=60,
                 *, enabled=True):
        from yt_playlist.rec import account_growth, home_vibe
        home_vibe.configure_access(lambda store, now: account_growth.vibe_access(store, now))
        self.ctx = ctx
        self.debounce_s = debounce_s
        self.discovery_tick_s = discovery_tick_s   # background discovery scan cadence (~30 min)
        self.gc_tick_s = gc_tick_s                 # generated-playlist GC cadence (daily)
        self.gc_initial_s = gc_initial_s           # first GC pass shortly after start (catches restarts)
        self._lock = threading.Lock()
        self.model_lock = threading.RLock()
        self.jobs = PendingJobs()
        self._running = False
        self._model_active = False
        self._discovering = False
        self._ticker_started = False
        self.enabled = enabled
        self._stop = threading.Event()
        self._threads = []
        self._mix_listening_check = 0.
        self._mix_selection_prepared_for = None
        self._story_check = float("-inf")
        self.work = activity_for(ctx)
        limit_native_threads()
        model_selection.recover_interrupted(ctx.store)
        from yt_playlist.rec.maintenance import Projections
        self.projections = Projections(ctx.store, ctx.now_fn, ctx.logger, accepting=self._accepting,
                                       schedule=self._schedule_projections, execute=self._stage)
        self.tasks = ServingTasks(ctx, self._stage, checkpoint=self._flush_measurements)
        from yt_playlist.rec.progressive import Preparation
        self.preparation = Preparation(ctx.store)
        from yt_playlist.rec.retention_maintenance import Retention
        self.retention = Retention(ctx.store, ctx.now_fn, ctx.logger, execute=self._stage,
                                   monotonic=lambda: time.monotonic())
        from yt_playlist.rec.model_refresh import FitCache
        self._fit_cache = FitCache()
        if self.enabled:
            with ctx.store._lock:
                pending = ctx.store.evidence_files.maintenance_state()['pending']
                ctx.store._evidence_recovery_pending = bool(pending)
            ctx.store.background_measurement = self.enqueue_measurement
            from yt_playlist.rec.choice_capture import CaptureQueue
            ctx.store.choice_capture = CaptureQueue(self.enqueue_measurement)

    def start_ticker(self):
        """Start the periodic background daemons (idempotent): the budgeted discovery scan (so the
        album/artist pools keep filling between syncs) and the daily generated-playlist GC sweep."""
        with self._lock:
            if self._ticker_started or not self.enabled:
                return
            self._stop.clear()
            self._ticker_started = True
        for target in (self._tick_loop, self._gc_loop, self._maintenance_loop):
            thread = threading.Thread(target=target, daemon=True)
            self._threads.append(thread)
            thread.start()

    def _maintenance_loop(self):
        """Notice committed changes from live playback and external import writers."""
        while not self._stop.wait(0.5):
            try:
                self._release_idle_preparation()
                self.ctx.store.maintenance_diagnostics.flush()
                self._poll_album_feedback()
                self._poll_evidence_storage()
                self._poll_retention()
                from yt_playlist.core import playback_feedback
                if playback_feedback.try_drain(self.ctx.store):
                    self.trigger_rating_update()
                self._poll_mix_selection()
                self._poll_mix_listening()
                self._retry_home()
                self._retry_bundles()
                self.projections.poll()
                self._poll_refresh()
                self._poll_choices()
                self._retry_measurements()
                self._poll_tv()
                self._poll_stories()
            except WorkClosing:
                # Idle and cancelled restart admission can reopen while this
                # rejection unwinds. Keep the periodic worker alive to resume.
                if self._stop.is_set() or self.work.closing_reason == 'shutdown':
                    return
            except Exception:
                self.ctx.logger.warning('recommendation change polling failed', exc_info=True)

    def _release_idle_preparation(self):
        # Storage pauses stop admissions, so an idle snapshot cannot wait for
        # the next capture to release its WAL view and file-reader leases.
        # Never close a generator while another thread is advancing it.
        if self.model_lock.acquire(blocking=False):
            try:
                self.preparation.expire_capture(paused=not self._accepting())
            finally:
                self.model_lock.release()

    def _poll_album_feedback(self):
        from yt_playlist.rec import album_feedback
        if not self.enabled or self._stop.is_set() or not self.work.accepting:
            return 0
        def drain():
            # Keep admission ownership through SQL cancellation translation.
            with cancellable_work(self._stop.is_set):
                return album_feedback.try_drain(self.ctx.store)
        return self.work.wrap('album feedback responses', drain)()

    def _poll_mix_listening(self):
        from yt_playlist.library import mix_listening

        if not self._accepting() or time.monotonic() < self._mix_listening_check:
            return
        self._mix_listening_check = time.monotonic() + max(0.5, self.debounce_s)

        def refresh():
            with cancellable_work(self._stop.is_set):
                return mix_listening.prepare(self.ctx.store, self.ctx.now_fn())

        return self.work.wrap(mix_listening.PHASE, refresh)()

    def _poll_mix_selection(self):
        from yt_playlist.core import recording_selection

        store = self.ctx.store
        connection = store.conn
        if connection is self._mix_selection_prepared_for or not self._accepting():
            return
        with cancellable_work(self._stop.is_set):
            if self.work.wrap('recording mix selection backfill', recording_selection.prepare, store)():
                self._mix_selection_prepared_for = connection

    def _poll_refresh(self):
        from yt_playlist.library.worker_composition import maintenance_commands
        commands = maintenance_commands(self)
        busy = self.busy
        now = time.monotonic()
        with self._lock:
            prepare = (self._accepting() and not self.jobs.has(Job.PREPARE)
                       and now >= self.jobs.retry[Job.PREPARE].retry_at)
        # A retried serving projection may already be queued by this ticker.
        # Content slices must join that serial queue: those projections can
        # depend on the very inputs that would otherwise never be prepared.
        if prepare and self.preparation.due(self.ctx.now_fn()):
            self._schedule(full=False, prepare=True)
        if busy:
            return
        group_state = self.jobs.retry[Job.GROUPS]
        if commands.groups_due(group_state.dirty, admitted=now >= group_state.retry_at):
            self._schedule(full=False, groups=True)
        state = self.jobs.retry[Job.FULL]
        if now >= state.retry_at and commands.full_due(state.dirty):
            self.trigger()
        if now >= self.jobs.retry[Job.BUNDLES].retry_at and commands.bundles_due():
            self._schedule(full=False, bundles=True)

    def _poll_choices(self):
        from yt_playlist.library.worker_composition import maintenance_commands
        if self.busy or time.monotonic() < self.jobs.retry[Job.CHOICES].retry_at:
            return
        if maintenance_commands(self).choices_due():
            self._schedule(full=False, choices=True)

    def _poll_tv(self):
        from yt_playlist.library.worker_composition import maintenance_commands
        state = self.jobs.retry[Job.TV]
        if self.jobs.has(Job.TV) or time.monotonic() < state.retry_at:
            return
        if maintenance_commands(self).tv_due(state.dirty):
            self._schedule(full=False, tv=True)

    def _poll_evidence_storage(self):
        state = self.jobs.retry[Job.EVIDENCE_STORAGE]
        if not self.jobs.has(Job.EVIDENCE_STORAGE) and time.monotonic() >= state.retry_at:
            self._schedule(full=False, evidence_storage=True)

    def _poll_retention(self):
        state = self.jobs.retry[Job.RETENTION]
        if not self.jobs.has(Job.RETENTION) and time.monotonic() >= state.retry_at:
            self._schedule(full=False, retention=True)

    def _retry_measurements(self):
        with self._lock:
            if (not self.enabled or self._stop.is_set() or not self.work.accepting
                    or getattr(self.ctx.store, 'activity_paused', False)
                    or self._running or not self.jobs.measurements
                    or time.monotonic() < self.jobs.retry[Job.MEASUREMENT].retry_at):
                return
            self._running = True
        self._start_loop()

    def shutdown(self):
        self._stop.set()
        with self._lock:
            offers = self.jobs.cancel()
        for offer in offers:
            offer.close()

    def join(self, timeout):
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self._threads):
            raise RuntimeError("Recommendation worker did not stop")
        self._threads.clear()
        self.preparation.close()
        from yt_playlist.rec import group_roots
        group_roots.close_retention(self.ctx.store)
        self.ctx.store.retention.close()
        self.ctx.store.__dict__.pop('_deferred_bundles', None)
        self._ticker_started = False
        self.ctx.store.maintenance_diagnostics.flush(force=True)

    def _tick_loop(self):
        delay = self.discovery_tick_s
        while not self._stop.wait(delay):
            delay = self.discovery_tick_s
            try:
                with self.model_lock:
                    self.work.wrap("discovery", self._discover, self.ctx.now_fn())()
            except WorkClosing:
                return
            except WorkBudgetExceeded:
                delay = min(60, self.discovery_tick_s)
                self.ctx.logger.info("Discovery deferred by read deadline; retrying in %s seconds", delay)
            except PolicyCapacityFull as exc:
                self._defer_policy_capacity(exc)
            except Exception:  # noqa: BLE001 - a scan failure must never crash the ticker
                self.ctx.logger.warning("discovery tick failed", exc_info=True)

    def _discover(self, now):
        from yt_playlist.rec import discover
        if getattr(self.ctx.store, '_recommendation_retirement_pending', False):
            return None
        self._discovering = True
        try:
            limit_native_threads()
            with self.ctx.store.priority.background(check_stopping), cancellable_work(self._stop.is_set):
                result = discover.run_discovery(self.ctx, now)
                # Discovery joins the same hourly preparation admission.
                check_stopping()
                self.trigger_candidate_refresh()
                return result
        finally:
            self._discovering = False

    def _gc_loop(self):
        """Daily collection plus due, persistent playlist retries between full sweeps."""
        from yt_playlist.library.worker_composition import collection_commands
        commands = collection_commands(self)
        if self._stop.wait(self.gc_initial_s):
            return
        next_sweep = time.monotonic()
        next_discovery = next_sweep
        next_collection = next_sweep
        while not self._stop.is_set():
            if getattr(self.ctx.store, '_recommendation_retirement_pending', False):
                if self._stop.wait(5):
                    return
                continue
            full_sweep = time.monotonic() >= next_sweep
            try:
                commands.generated(full_sweep)
            except WorkClosing:
                return
            except Exception:  # noqa: BLE001 - a GC failure must never crash the daemon
                self.ctx.logger.warning("generated-playlist GC tick failed", exc_info=True)
            if time.monotonic() >= next_collection:
                try:
                    commands.recovery()
                except WorkClosing:
                    return
                except Exception:
                    self.ctx.logger.warning('Collection recovery cleanup deferred',exc_info=True)
                next_collection = time.monotonic() + (
                    min(60, self.gc_tick_s) if commands.recovery_pending()
                    else self.gc_tick_s)
            if full_sweep:
                next_sweep = time.monotonic() + self.gc_tick_s
            if time.monotonic() >= next_discovery:
                discovery_delay = self.gc_tick_s
                try:               # #52: discovery collection keeps its daily cadence
                    commands.discoveries()
                except WorkClosing:
                    return
                except WorkBudgetExceeded as exc:
                    self.ctx.logger.info("GC: discovery pool deferred: %s", exc)
                    discovery_delay = min(60, self.gc_tick_s)
                except Exception:  # noqa: BLE001 - discovery GC must never crash the daemon
                    self.ctx.logger.warning("discovery-pool GC tick failed", exc_info=True)
                next_discovery = time.monotonic() + discovery_delay
            delay = max(0, min(next_sweep, next_discovery, next_collection) - time.monotonic())
            try:
                retry_at = commands.next_retry()
                if retry_at is not None:
                    # A disconnected account or contended claim must not cause a busy loop.
                    delay = min(delay, max(60, retry_at - self.ctx.now_fn()))
            except WorkClosing:
                return
            except Exception:
                self.ctx.logger.warning("could not read playlist GC retry deadline", exc_info=True)
            if self._stop.wait(delay):
                return

    @property
    def busy(self):
        """True while a rebuild is scheduled or running, driving the 'refreshing…' UI state."""
        with self._lock:
            return self._model_active or self.jobs.visible

    @property
    def preparing(self):
        """Keep the preparation notice visible between retries of runtime output."""
        if self.busy:
            return True
        with self._lock:
            return any(self.jobs.retry[job].dirty for job in
                (Job.PREPARE, Job.FULL, Job.MODES, Job.BUNDLES, Job.HOME))

    def enqueue_measurement(self, offer):
        """Use this worker for analytics too; requests only pin and enqueue their offered state."""
        with self._lock:
            if (self._stop.is_set() or not self.work.accepting
                    or getattr(self.ctx.store, 'activity_paused', False)):
                return False
            if not self.jobs.merge(Job.MEASUREMENT, offer):
                self.ctx.logger.warning("recommendation measurement backlog full; offer omitted: surface=%s",
                                        offer.surface)
                return False
            start = (not self._running
                     and time.monotonic() >= self.jobs.retry[Job.MEASUREMENT].retry_at)
            if start:
                self._running = True
        if start:
            self._start_loop()
        return True

    def _flush_measurements(self):
        with self._lock:
            state = self.jobs.retry[Job.MEASUREMENT]
            if time.monotonic() < state.retry_at:
                return
            offers = self.jobs.take_measurements(limit=8)
        deferred = []
        try:
            for index, offer in enumerate(offers):
                storage_full = None
                try:
                    with (self.ctx.store.priority.background(check_stopping),
                          cancellable_work(self._stop.is_set), work_span("rec: serving measurement")):
                        check_stopping()
                        try:
                            offer.run()
                        except EvidenceBudgetExceeded as exc:
                            storage_full = exc
                            from yt_playlist.rec import choice_evidence
                            choice_evidence.maintain_storage(self.ctx.store, self.ctx.now_fn(),
                                                            required_bytes=exc.required_bytes)
                            storage_full = None
                            offer.run()
                except WorkClosing:
                    raise
                except EvidenceBudgetExceeded as exc:
                    # Responses behind this offer must not run before its durable
                    # write. Keep the batch tail ahead of later arrivals, with its
                    # original context pins and admission slots.
                    deferred = offers[index:]
                    self.ctx.logger.warning(
                        'Listening capture deferred for retry: surface=%s required_bytes=%s '
                        'used_bytes=%s budget_bytes=%s', offer.surface, exc.required_bytes,
                        exc.used_bytes, exc.budget_bytes)
                    break
                except Exception:
                    if storage_full is not None:
                        deferred = offers[index:]
                        self.ctx.logger.warning('Listening capture retained after storage maintenance '
                                                'failed: surface=%s', offer.surface, exc_info=True)
                        break
                    self.ctx.logger.warning('Listening capture failed: surface=%s', offer.surface, exc_info=True)
        finally:
            with self._lock:
                # A recovery pause prevents new admissions. Captures already
                # accepted still own their FIFO position and lifetime pins.
                retained = deferred if self.enabled and not self._stop.is_set() and self.work.accepting else []
                self.jobs.finish_measurements(offers, retry=retained)
                if retained:
                    state.failed(time.monotonic(), base=5, cap=300, exponent_cap=6)
                elif offers:
                    state.succeeded()
            for offer in offers:
                if not any(offer is pending for pending in retained):
                    offer.close()

    @property
    def discovery_busy(self):
        """Include independent discovery ticks while keeping their last prepared picks visible."""
        return self.busy or self._discovering

    def trigger(self):
        """Coalesce source changes into a background refresh of the existing models."""
        self._schedule(full=True, prepare=True)

    def trigger_mode_refresh(self):
        """Initialize missing genre groups and bundles; reuse an already prepared model."""
        self._schedule(full=False, modes=True)

    def trigger_rating_update(self):
        """Resolve exact-video rating subjects without blocking the bridge's response reader."""
        self._schedule(full=False, ratings=True)

    def trigger_candidate_refresh(self):
        """Queue control changes for the next admitted preparation round."""
        self._schedule(full=False, bundles=True)

    def trigger_home_refresh(self, *, force=False):
        """Coalesce disposable Home preparation without asking for any model fit.

        Automatic preparation shares a thirty-second backoff, including bundle
        publications during catch-up. The maintenance ticker retries dirty work
        even when no Home request arrives. Forced refreshes skip the backoff.
        """
        with self._lock:
            state = self.jobs.retry[Job.HOME]
            state.dirty = True
        if force or time.monotonic() - state.attempted_at >= 30:
            self._schedule(full=False, home=True)

    def _retry_home(self):
        with self._lock:
            state = self.jobs.retry[Job.HOME]
            due = state.dirty and time.monotonic() - state.attempted_at >= 30
        if due:
            self._schedule(full=False, home=True)

    def _retry_bundles(self):
        """Retry a failed projection even after the learning backlog has drained."""
        from yt_playlist.rec import candidate_refresh
        with self._lock:
            state = self.jobs.retry[Job.BUNDLES]
            due = state.dirty and time.monotonic() >= state.retry_at
        if due and candidate_refresh.check_due(self.ctx.store, self.ctx.now_fn()):
            self._schedule(full=False, bundles=True)

    def trigger_content_update(self, keys=(), *, video_ids=()):
        """Coalesce metadata changes for the next background model and pool preparation."""
        keys = set(keys)
        keys.update(key for vid in video_ids if (key := self.ctx.store.tracks.identity_key_for_video(vid)))
        if keys:
            self.ctx.store.rec_refresh.enqueue()
            self._schedule(full=True, prepare=True)

    def trigger_story_refresh(self):
        """A cold reading tab queues article preparation, never a synchronous provider lookup."""
        if time.monotonic() - self.jobs.retry[Job.STORIES].attempted_at >= STORY_CHECK_INTERVAL_S:
            return self._schedule(full=False, stories=True)

    def _poll_stories(self):
        now = time.monotonic()
        if not self._accepting() or now < self._story_check or self.busy:
            return
        if now - self.jobs.retry[Job.STORIES].attempted_at < STORY_CHECK_INTERVAL_S:
            return
        self._story_check = now + STORY_CHECK_INTERVAL_S
        if self.tasks.stories_due():
            self.trigger_story_refresh()

    def trigger_trends_refresh(self):
        """Queue the monthly recap through its coalescing summary owner."""
        self.projections.request('trends')

    def trigger_artist_images(self, artist_ids):
        """Coalesce on-screen artwork repairs separately from model and Home preparation."""
        if artist_ids:
            self._schedule(full=False, artist_images=artist_ids)

    def _accepting(self):
        return (self.enabled and not self._stop.is_set() and self.work.accepting
                and not getattr(self.ctx.store, 'activity_paused', False))

    def _schedule_projections(self, kinds):
        return self._schedule(full=False, **dict.fromkeys(kinds, True))

    def trigger_analytics_refresh(self):
        from yt_playlist.core.features import listening_charts_enabled
        return listening_charts_enabled() and self._schedule(full=False, analytics=True)

    def trigger_health_refresh(self):
        self.projections.request('model_health')

    @property
    def health_refresh_failed(self):
        return self.projections.failed('model_health')

    def trigger_tv_refresh(self):
        from yt_playlist.rec import tv
        tv.refresh(self.ctx.store)
        with self._lock:
            state = self.jobs.retry[Job.TV]
            state.dirty = True
        if time.monotonic() >= state.retry_at:
            return self._schedule(full=False, tv=True)
        return False

    def _schedule(self, *, full, modes=False, keys=(), ratings=False, trends=False, cleanup=False, bundles=False, home=False,
                  stories=False, artist_images=(), prepare=False, choices=False, analytics=False, groups=False, tv=False,
                  model_health=False, library_taste=False, evidence_storage=False, retention=False):
        with self._lock:
            if getattr(self.ctx.store, '_recommendation_retirement_pending', False):
                if not retention or time.monotonic() < self.jobs.retry[Job.RETENTION].retry_at:
                    return False
            recovery = ((evidence_storage or retention) and self.enabled and not self._stop.is_set()
                        and self.work.accepting
                        and not getattr(self.ctx.store, '_activity_paused', False))
            if not self._accepting() and not recovery:
                return False
            requested = {Job.FULL: full, Job.MODES: modes, Job.RATINGS: ratings, Job.PREPARE: prepare, Job.TRENDS: trends,
                         Job.CLEANUP: cleanup, Job.BUNDLES: bundles, Job.HOME: home, Job.STORIES: stories,
                         Job.ARTWORK: artist_images, Job.CONTENT: keys, Job.CHOICES: choices, Job.ANALYTICS: analytics,
                         Job.GROUPS: groups, Job.TV: tv, Job.MODEL_HEALTH: model_health, Job.LIBRARY_TASTE: library_taste,
                         Job.EVIDENCE_STORAGE: evidence_storage, Job.RETENTION: retention}
            for job, payload in requested.items():
                if payload:
                    self.jobs.merge(job, payload)
            if any(requested.values()) and self.ctx.logger.isEnabledFor(logging.DEBUG):
                caller = traceback.extract_stack(limit=3)[0]
                self.ctx.logger.debug("rec scheduled: kind=%s changed_tracks=%d running=%s source=%s:%d in %s",
                    "models" if full else ("ratings" if ratings else "trends" if trends else
                                          "bundles" if bundles else "initialization" if modes else "content"),
                    len(self.jobs.pending.get(Job.CONTENT, ())), self._running,
                    caller.filename, caller.lineno, caller.name)
            if self._running:
                return True
            self._running = True
        self._start_loop()
        return True

    def _start_loop(self):
        try:
            run = self.work.wrap("recommendation updates", self._loop)
        except WorkClosing:
            self._running = False
            self.shutdown()
            return
        thread = threading.Thread(target=run, daemon=True)
        self._threads.append(thread)
        try:
            thread.start()
        except BaseException:
            run.release()
            self._running = False
            raise

    def _loop(self):
        try:
            limit_native_threads()
            self._run_pending()
        except WorkClosing:
            # Application admission may close before shutdown() is called.
            # Release arrivals and their pinned readers on either stop path.
            self.shutdown()
            with self._lock:
                self._model_active = False
                self._running = False
            self.ctx.logger.info("rec worker stopped for shutdown")
        except Exception:
            # A failed error-report write must not leave a dead scheduler owning
            # arrivals forever. Keep queued intent for the next trigger/poll.
            with self._lock:
                self._model_active = False
                self._running = False
            self.ctx.logger.warning('Recommendation worker stopped unexpectedly; '
                                    'pending work retained', exc_info=True)
        except BaseException:
            with self._lock:
                self._model_active = False
                self._running = False
            raise

    def _run_pending(self):
        from yt_playlist.library.worker_composition import pending_commands
        from yt_playlist.rec.worker_dispatch import PendingDispatch
        return PendingDispatch(self.jobs, pending_commands(self), stop=self._stop,
            debounce=self.debounce_s, lock=self._lock, model_lock=self.model_lock,
            flush=self._flush_measurements, schedule=self._schedule,
            active=lambda value: setattr(self, '_model_active', value),
            running=lambda value: setattr(self, '_running', value),
            clock=lambda: time.monotonic(), logger=self.ctx.logger).run()

    def rebuild(self):
        """Run maintenance directly for background callers and command-line tools.

        Interactive handlers use trigger(). An active worker absorbs direct calls
        into its next pass instead of fitting a second model concurrently.
        Successful publication may enqueue Home and shadow-group continuations;
        Activity owns those until completion even when model-facing busy is false.
        Callers that own this worker must shut down and join it before disposal.
        """
        with self._lock:
            if self._stop.is_set() or not self.work.accepting:
                return
            if self._running:
                self.jobs.merge(Job.FULL)   # one's already in flight; absorb this request
                return
            self._running = True
            self._model_active = True
        try:
            self.work.wrap("recommendation rebuild", self._do_rebuild)()
        finally:
            with self._lock:
                again = bool(self.jobs)
                self._running = again
                self._model_active = False
            if again:
                self._start_loop()

    def _stage(self, name, target, *args, **kwargs):
        limit_native_threads()
        if name != 'recommendation cleanup' and not getattr(self.ctx.store, '_recommendation_retirement_pending', False):
            self._flush_measurements()
        with (self.ctx.store.priority.background(check_stopping), cancellable_work(self._stop.is_set),
              work_span("rec: " + name, announce=True, level=logging.DEBUG)):
            return target(*args, **kwargs)

    def _do_mode_refresh(self):
        with self.model_lock:
            self._refresh_modes()

    def _refresh_modes(self):
        """Serve saved results immediately; prepare missing or changed state in the worker."""
        if self.ctx.store.rec_refresh.has_pending_changes() and self.preparation.full_due(self.ctx.now_fn()):
            self._rebuild()
        else:
            self._refresh_bundles()

    def _prepare_content(self):
        """One bounded slice, then yield to ratings, summaries and foreground work."""
        state = self.jobs.retry[Job.PREPARE]
        if time.monotonic() < state.retry_at:
            return
        state.started(time.monotonic())
        with self.model_lock:
            now = self.ctx.now_fn()
            captured = self._stage('content input slice', self.preparation.capture, now)
            fitted = self._stage('content basis step', self.preparation.fit, now)
        state.succeeded()
        return captured or fitted

    def _prepare_groups(self):
        """Shadow work runs after serving jobs; background arrivals cannot starve it."""
        from yt_playlist.library.worker_composition import group_commands
        state = self.jobs.retry[Job.GROUPS]
        if time.monotonic() < state.retry_at:
            state.dirty = True
            return
        state.started(time.monotonic())

        with self.model_lock:
            groups = self.ctx.store.taste_groups
            previous_head = groups.head_generation()
            outcome = group_commands(self).prepare()
            groups_changed = groups.head_generation() != previous_head
        if outcome.state == 'deferred':
            state.deferred(time.monotonic())
        elif outcome.state == 'failed':
            state.failed(time.monotonic(), base=60, cap=3600)
        else:
            state.succeeded()
        if groups_changed or outcome.refresh_home:
            self._schedule(full=False, home=True, bundles=groups_changed)

    def _do_rebuild(self):
        # Source removal waits for publication, then clears every derived cache
        # before scheduling a build from the remaining evidence.
        with self.model_lock:
            self._rebuild()

    def _refresh_trends(self):
        return self.projections.run('trends')

    def _refresh_bundles(self):
        from yt_playlist.rec import candidate_refresh, mode_surfaces, recommendation_retirement
        store = self.ctx.store
        # Model fitting can succeed while ranking fails. Keep the pool's retry
        # clock separate so it cannot stall ratings or reset its own backoff.
        with self._lock:
            state = self.jobs.retry[Job.BUNDLES]
            if (time.monotonic() < state.retry_at
                    or not candidate_refresh.check_due(store, self.ctx.now_fn())):
                state.dirty = True
                return None
        # A failed cleanup has not started ranking and must not consume its
        # hourly retry interval. Direct preparation callers share this gate.
        self._stage('recommendation cleanup', recommendation_retirement.drain, store, self.ctx.now_fn())
        with self._lock:
            state.dirty = False
            state.started(time.monotonic())
        try:
            result = self._stage("cluster surfaces and PPR", mode_surfaces.prepare_scheduled,
                                 store, self.ctx.now_fn())
        except WorkClosing:
            raise
        except PolicyCapacityFull as exc:
            self._defer_policy_capacity(exc)
            return None
        except Exception:
            with self._lock:
                backoff = state.failed(time.monotonic(), base=5, cap=300, exponent_cap=6)
            self.ctx.logger.warning('rec bundle preparation failed: attempt=%d retry_backoff=%.1fs; '
                                    'retaining published pools', state.failures, backoff, exc_info=True)
            return None
        if result is None:
            self.ctx.logger.debug("rec mode bundles deferred; retaining published pools")
            with self._lock:
                state.deferred(time.monotonic())
        else:
            with self._lock:
                state.succeeded()
            self.ctx.logger.debug("rec rebuild: mode bundles prepared")
            # Assemble the captured output through Home's existing worker.
            self.trigger_home_refresh()
            self._schedule(full=False, groups=True)
        return result

    def _defer_policy_capacity(self, exc):
        with self._lock:
            state = self.jobs.retry[Job.BUNDLES]
            now = time.monotonic()
            state.deferred(now)
            # A long ranking already consumed the start-to-start interval.
            # Leave an entire interval after rejection for serial retention
            # turns, even while preparation changes invalidate the cached plan.
            state.retry_at = max(state.retry_at, now + state.minimum_interval)
            self.jobs.retry[Job.RETENTION].retry_at = min(
                self.jobs.retry[Job.RETENTION].retry_at, now)
        self.ctx.logger.info('Recommendation publication deferred for policy capacity: %s', exc)

    def _rebuild(self):
        """Fit privately; keep the last completed model available throughout the work."""
        from yt_playlist.library.worker_composition import model_commands
        with self._lock:
            state = self.jobs.retry[Job.FULL]
            if time.monotonic() < state.retry_at:
                state.dirty = True
                return
            state.started(time.monotonic())
        command = model_commands(self)
        result = command.fit()
        if result is None:
            self.ctx.logger.debug('rec models deferred: inputs changed during fitting; retaining published model')
            with self._lock:
                state.deferred(time.monotonic())
            # Candidate caches can use the last coherent model while metadata is
            # still arriving. Their independent retry clock bounds ranking work.
            self._refresh_bundles()
            return
        with self._lock:
            state.succeeded()
        command.complete(result[1])
        self._refresh_bundles()
