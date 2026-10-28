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

from yt_playlist.rec import model_selection
from yt_playlist.core.activity import (activity_for, cancellable_work, check_stopping, WorkClosing,
                                       work_budget, WorkBudgetExceeded)
from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.core.numerics import limit_native_threads
from yt_playlist.core.evidence_ledger import EvidenceBudgetExceeded
from yt_playlist.rec.worker_queue import Job, PendingJobs
from yt_playlist.rec.worker_tasks import ServingTasks

# A continuing metadata backlog must not cancel every graph attempt. User-facing
# choices still preempt its bounded background slice.
GROUP_PREEMPT = frozenset({Job.HOME, Job.MODES, Job.RATINGS, Job.CLEANUP})


class RecWorker:
    def __init__(self, ctx, debounce_s=2.0, discovery_tick_s=1800, gc_tick_s=86400, gc_initial_s=60,
                 *, enabled=True):
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
        self._group_yield = threading.Event()
        self._threads = []
        self.work = activity_for(ctx)
        limit_native_threads()
        model_selection.recover_interrupted(ctx.store)
        from yt_playlist.rec.maintenance import Projections
        self.projections = Projections(ctx.store, ctx.now_fn, ctx.logger, accepting=self._accepting,
                                       schedule=self._schedule_projections, execute=self._stage)
        self.tasks = ServingTasks(ctx, self._stage, checkpoint=self._flush_measurements)
        from yt_playlist.rec.progressive import Preparation
        self.preparation = Preparation(ctx.store)
        from yt_playlist.rec.model_refresh import FitCache
        self._fit_cache = FitCache()
        if self.enabled:
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
                self.ctx.store.maintenance_diagnostics.flush()
                self._retry_home()
                self._retry_bundles()
                self.projections.poll()
                self._poll_refresh()
                self._poll_choices()
                self._poll_evidence_storage()
                self._retry_measurements()
                self._poll_tv()
            except Exception:
                self.ctx.logger.warning('recommendation change polling failed', exc_info=True)

    def _poll_refresh(self):
        from yt_playlist.rec import candidate_refresh
        if self.busy:
            return
        now = time.monotonic()
        if (now >= self.jobs.retry[Job.PREPARE].retry_at
                and self.preparation.due(self.ctx.now_fn())):
            self._schedule(full=False, prepare=True)
        group_state = self.jobs.retry[Job.GROUPS]
        if self.ctx.store.settings.get_setting('rec_group_shadow') == '0':
            from yt_playlist.rec import group_serving
            self.ctx.store.maintenance_diagnostics.resolve_wait(group_serving.PHASE, 'disabled')
        elif now >= group_state.retry_at:
            from yt_playlist.rec import group_preparation
            due = (group_state.dirty or self.preparation.groups.due(self.ctx.now_fn())
                   or group_preparation.status(self.ctx.store).get('phase') in {'failed', 'preparing', 'deferred'})
            if not due and (generation := self.ctx.store.taste_groups.head_generation()):
                from yt_playlist.rec import group_serving
                preview = self.ctx.store.rec_proposals.get_proposals(group_serving.SURFACE)
                due = not preview or preview['generation'] != generation or preview.get('schema') != group_serving.SCHEMA
            if due:
                self._schedule(full=False, groups=True)
        state = self.jobs.retry[Job.FULL]
        if state.dirty or self.ctx.store.rec_refresh.has_pending_changes():
            if now >= state.retry_at and self.preparation.full_due(self.ctx.now_fn()):
                self.trigger()
        if (now >= self.jobs.retry[Job.BUNDLES].retry_at
              and (not self.ctx.store.rec_proposals.has_proposals('mode_bundles')
                   or candidate_refresh.due(self.ctx.store, self.ctx.now_fn()))):
            # Decay/expiry changes rankings, not model fitting inputs.
            self._schedule(full=False, bundles=True)

    def _poll_choices(self):
        from yt_playlist.rec import choice_evidence, daily_model_selection
        if self.busy or time.monotonic() < self.jobs.retry[Job.CHOICES].retry_at:
            return
        if (choice_evidence.daily_due(self.ctx.store, self.ctx.now_fn())
                or daily_model_selection.due(self.ctx.store, self.ctx.now_fn())):
            self._schedule(full=False, choices=True)

    def _poll_tv(self):
        from yt_playlist.rec import tv
        state = self.jobs.retry[Job.TV]
        if self.jobs.has(Job.TV) or time.monotonic() < state.retry_at:
            return
        pool = tv.cached(self.ctx.store)
        now = self.ctx.now_fn()
        retry_due = pool and now >= pool.get('retry_at', pool['at'] + tv.SEARCH_RETRY_S)
        if state.dirty or not pool or retry_due or not 0 <= now - pool['at'] < tv.MAX_AGE:
            self._schedule(full=False, tv=True)

    def _poll_evidence_storage(self):
        state = self.jobs.retry[Job.EVIDENCE_STORAGE]
        if not self.jobs.has(Job.EVIDENCE_STORAGE) and time.monotonic() >= state.retry_at:
            self._schedule(full=False, evidence_storage=True)

    def _retry_measurements(self):
        with self._lock:
            if (not self._accepting() or self._running or not self.jobs.measurements
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
        self._ticker_started = False
        self.ctx.store.maintenance_diagnostics.flush(force=True)

    def _tick_loop(self):
        while not self._stop.wait(self.discovery_tick_s):
            try:
                with self.model_lock:
                    self.work.wrap("discovery", self._discover, self.ctx.now_fn())()
            except WorkClosing:
                return
            except Exception:  # noqa: BLE001 - a scan failure must never crash the ticker
                self.ctx.logger.warning("discovery tick failed", exc_info=True)

    def _discover(self, now):
        from yt_playlist.rec import discover, fresh_pool
        self._discovering = True
        try:
            limit_native_threads()
            with self.ctx.store.priority.background(check_stopping), cancellable_work(self._stop.is_set):
                result = discover.run_discovery(self.ctx, now)
                # The scan runs after the rebuild's earlier Home bundles. Publish the new songs now,
                # rather than waiting for another rebuild or making a page request rank them.
                check_stopping()
                fresh_pool.prepare(self.ctx.store, now)
                self.trigger_home_refresh()
                return result
        finally:
            self._discovering = False

    def _gc_loop(self):
        """Daily collection plus due, persistent playlist retries between full sweeps."""
        from yt_playlist.library import playlist_generated, playlist_gc_retry
        from yt_playlist.rec import discover
        if self._stop.wait(self.gc_initial_s):
            return
        next_sweep = time.monotonic()
        next_discovery = next_sweep
        while not self._stop.is_set():
            full_sweep = time.monotonic() >= next_sweep
            try:
                clients = self.ctx.client_provider() or {}
                if clients:        # no clients configured (e.g. pre-setup) -> nothing to delete remotely
                    kwargs = {} if full_sweep else {"retry_only": True}
                    collected = self.work.wrap("generated-playlist collection",
                        playlist_generated.gc_generated_playlists, self.ctx.store, clients,
                        self.ctx.now_fn(), **kwargs)()
                    if collected:
                        self.ctx.logger.info("GC: collected %d unplayed generated playlist(s): %s",
                                             len(collected), ", ".join(c["title"] for c in collected))
            except WorkClosing:
                return
            except Exception:  # noqa: BLE001 - a GC failure must never crash the daemon
                self.ctx.logger.warning("generated-playlist GC tick failed", exc_info=True)
            if full_sweep:
                next_sweep = time.monotonic() + self.gc_tick_s
            if time.monotonic() >= next_discovery:
                discovery_delay = self.gc_tick_s
                try:               # #52: discovery collection keeps its daily cadence
                    gc = self.work.wrap("discovery collection", discover.gc_discovery,
                                        self.ctx, self.ctx.now_fn())()
                    if any(gc.values()):
                        self.ctx.logger.info("GC: discovery pool removed %s", gc)
                except WorkClosing:
                    return
                except WorkBudgetExceeded as exc:
                    self.ctx.logger.info("GC: discovery pool deferred: %s", exc)
                    discovery_delay = min(60, self.gc_tick_s)
                except Exception:  # noqa: BLE001 - discovery GC must never crash the daemon
                    self.ctx.logger.warning("discovery-pool GC tick failed", exc_info=True)
                next_discovery = time.monotonic() + discovery_delay
            delay = max(0, min(next_sweep, next_discovery) - time.monotonic())
            try:
                retry_at = playlist_gc_retry.next_retry_at(self.ctx.store)
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

    def enqueue_measurement(self, offer):
        """Use this worker for analytics too; requests only pin and enqueue their offered state."""
        with self._lock:
            if self._stop.is_set() or not self.work.accepting or getattr(self.ctx.store, 'activity_paused', False):
                return False
            if not self.jobs.merge(Job.MEASUREMENT, offer):
                self.ctx.logger.warning("recommendation measurement backlog full; offer omitted: surface=%s",
                                        offer.surface)
                return False
            self._group_yield.set()
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
                retained = deferred if self._accepting() else []
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
        """Rerank completed models after a Taste control edit, reset or restore."""
        self._schedule(full=False, bundles=True)

    def trigger_home_refresh(self, *, force=False):
        """Coalesce disposable Home preparation without asking for any model fit.

        Automatic preparation shares a thirty-second backoff, including bundle
        publications during catch-up. The maintenance ticker retries dirty work
        even when no Home request arrives. Explicit completed pool jobs may force
        a refresh so their user-requested result becomes available immediately.
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
        with self._lock:
            state = self.jobs.retry[Job.BUNDLES]
            due = state.dirty and time.monotonic() >= state.retry_at
        if due:
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
        if time.monotonic() - self.jobs.retry[Job.STORIES].attempted_at >= 30:
            self._schedule(full=False, stories=True)

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
        with self._lock:
            state = self.jobs.retry[Job.TV]
            state.dirty = True
        if time.monotonic() >= state.retry_at:
            return self._schedule(full=False, tv=True)
        return False

    def _schedule(self, *, full, modes=False, keys=(), ratings=False, trends=False, cleanup=False, bundles=False, home=False,
                  stories=False, artist_images=(), prepare=False, choices=False, analytics=False, groups=False, tv=False,
                  model_health=False, evidence_storage=False):
        with self._lock:
            if not self._accepting():
                return False
            requested = {Job.FULL: full, Job.MODES: modes, Job.RATINGS: ratings, Job.PREPARE: prepare, Job.TRENDS: trends,
                         Job.CLEANUP: cleanup, Job.BUNDLES: bundles, Job.HOME: home, Job.STORIES: stories,
                         Job.ARTWORK: artist_images, Job.CONTENT: keys, Job.CHOICES: choices, Job.ANALYTICS: analytics,
                         Job.GROUPS: groups, Job.TV: tv, Job.MODEL_HEALTH: model_health,
                         Job.EVIDENCE_STORAGE: evidence_storage}
            for job, payload in requested.items():
                if payload:
                    self.jobs.merge(job, payload)
                    if job in GROUP_PREEMPT:
                        self._group_yield.set()
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
                self._running = False
            self.ctx.logger.info("rec worker stopped for shutdown")
        finally:
            self._model_active = False

    def _run_pending(self):
        while not self._stop.wait(self.debounce_s):
            self._flush_measurements()
            with self._lock:
                if not self.jobs:
                    self._running = False
                    return
                batch = self.jobs.drain()
                if (not batch and self.jobs.measurements
                        and time.monotonic() < self.jobs.retry[Job.MEASUREMENT].retry_at):
                    # The maintenance ticker restarts due captures. A capacity
                    # backlog must not keep this worker spinning between retries.
                    self._running = False
                    return
                self._model_active = any(job not in {Job.MEASUREMENT, Job.EVIDENCE_STORAGE, Job.CHOICES, Job.ANALYTICS, Job.GROUPS, Job.TV, Job.MODEL_HEALTH} for job in batch)
            full, modes = Job.FULL in batch, Job.MODES in batch
            if Job.EVIDENCE_STORAGE in batch:
                from yt_playlist.rec import choice_evidence
                state = self.jobs.retry[Job.EVIDENCE_STORAGE]
                state.retry_at = time.monotonic() + 300
                try:
                    self._stage('listening evidence storage', choice_evidence.maintain_storage,
                                self.ctx.store, self.ctx.now_fn())
                except WorkClosing:
                    raise
                except Exception:
                    self.ctx.logger.warning('Listening evidence maintenance failed', exc_info=True)
            if full and time.monotonic() < self.jobs.retry[Job.FULL].retry_at:
                # Keep the intent for the ticker without delaying ratings/serving jobs.
                self.jobs.retry[Job.FULL].dirty = True
                full = False
            keys = batch.get(Job.CONTENT, set())
            if Job.RATINGS in batch:
                try:
                    keys.update(self.tasks.ratings())
                except WorkClosing:
                    raise
                except Exception:
                    self.ctx.logger.warning("rating subject update failed", exc_info=True)
            preparation_refresh = False
            try:
                if Job.PREPARE in batch:
                    preparation_refresh = self._prepare_content()
                    # The later refresh in this batch already consumes this slice.
                    if preparation_refresh and Job.BUNDLES not in batch:
                        self._schedule(full=False, bundles=True)
                        preparation_refresh = False
                if full and not self.preparation.full_due(self.ctx.now_fn()):
                    self.jobs.retry[Job.FULL].dirty = False
                    full = False
                if full:
                    self._do_rebuild()           # includes genre groups and Home bundles
                elif modes:
                    self._do_mode_refresh()
                if Job.BUNDLES in batch:
                    with self.model_lock:
                        self._refresh_bundles()
                    preparation_refresh = False
            except WorkClosing:
                raise
            except Exception:  # noqa: BLE001 - never let rec work crash the app
                if preparation_refresh:
                    self._schedule(full=False, bundles=True)
                # A queued continuation or the maintenance poll must not turn a
                # persistent failure into a 10ms retry loop. Keep every traceback.
                backoff = self.jobs.retry[Job.FULL].failed(time.monotonic(), base=2, exponent_cap=4)
                self.ctx.logger.warning("rec worker rebuild failed: retry_backoff=%.1fs", backoff, exc_info=True)
                if full and modes:              # a failed full rebuild must not swallow local repair
                    self.trigger_mode_refresh()
            # Disposable projections keep their own retry clocks. A failed or
            # coalesced learning pass must not swallow either summary job.
            if Job.TRENDS in batch:
                with self.model_lock:
                    self._refresh_trends()
            if Job.CLEANUP in batch:
                with self.model_lock:
                    self.projections.run('cleanup')
            if Job.ANALYTICS in batch:
                with self.model_lock:
                    self.projections.run('analytics')
            if Job.MODEL_HEALTH in batch:
                with self.model_lock:
                    self.projections.run('model_health')
            if keys:
                try:
                    self.ctx.store.rec_refresh.enqueue()
                    self._schedule(full=True, prepare=True)
                except WorkClosing:
                    raise
                except Exception:
                    self.ctx.logger.warning("rec content refresh failed: tracks=%d", len(keys), exc_info=True)
            if Job.HOME in batch:
                try:
                    state = self.jobs.retry[Job.HOME]
                    state.attempted_at = time.monotonic()
                    with self.model_lock:
                        dirty = self.tasks.home()
                    with self._lock:
                        state.dirty |= dirty
                    self.trigger_tv_refresh()
                except WorkClosing:
                    raise
                except Exception:
                    with self._lock:
                        self.jobs.retry[Job.HOME].dirty = True
                    self.ctx.logger.warning("Home preparation failed; retaining previous collections", exc_info=True)
            if Job.CHOICES in batch:
                state = self.jobs.retry[Job.CHOICES]
                state.started(time.monotonic())
                try:
                    with self.model_lock:
                        active = self.ctx.store.settings.get_setting('rec_selection_active_run')
                        result = self.tasks.choices()
                        if (active != self.ctx.store.settings.get_setting('rec_selection_active_run')
                                or result and result.get('published_vectors')):
                            self._schedule(full=False, bundles=True, home=True)
                    state.succeeded()
                except WorkClosing:
                    raise
                except Exception:
                    state.failed(time.monotonic(), base=60, cap=3600)
                    self.ctx.logger.warning("Daily listening evaluation failed; retaining previous report", exc_info=True)
            if Job.ARTWORK in batch:
                try:
                    self.tasks.artwork(batch[Job.ARTWORK])
                except WorkClosing:
                    raise
                except Exception:
                    self.ctx.logger.warning('Artist image repair failed', exc_info=True)
            if Job.STORIES in batch:
                try:
                    self.jobs.retry[Job.STORIES].attempted_at = time.monotonic()
                    if self.tasks.stories():
                        self._schedule(full=False, stories=True)
                except WorkClosing:
                    raise
                except Exception:
                    self.ctx.logger.warning("Story preparation failed; retaining previous stories", exc_info=True)
            if Job.TV in batch:
                state = self.jobs.retry[Job.TV]
                if time.monotonic() >= state.retry_at:
                    state.started(time.monotonic())
                    try:
                        more = self.tasks.tv()
                        state.succeeded()
                        state.dirty = more
                        if more:
                            from yt_playlist.rec import tv
                            if len(tv.queue(self.ctx.store, self.ctx.now_fn())) < tv.QUEUE_SIZE:
                                # Fill a usable session without a minute between
                                # each small batch of searches. Failures retain
                                # the normal provider backoff below.
                                state.retry_at = min(state.retry_at, state.attempted_at + 5)
                    except WorkClosing:
                        raise
                    except Exception:
                        state.failed(time.monotonic(), base=60, cap=3600)
                        self.ctx.logger.warning('TV preparation failed; retaining previous videos', exc_info=True)
            if Job.GROUPS in batch:
                self._prepare_groups()
            self._model_active = False
        self._running = False

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
        from yt_playlist.rec import group_preparation, group_serving
        state = self.jobs.retry[Job.GROUPS]
        if time.monotonic() < state.retry_at:
            state.dirty = True
            return
        state.started(time.monotonic())

        with self._lock:
            if GROUP_PREEMPT.intersection(self.jobs.pending):
                state.deferred(time.monotonic())
                return
            self._group_yield.clear()
        if self.ctx.store.settings.get_setting('rec_group_shadow') == '0':
            self.ctx.store.maintenance_diagnostics.resolve_wait(group_serving.PHASE, 'disabled')
            state.succeeded()
            return

        def should_stop():
            # SQL cancellation callbacks can run under the database lock. They
            # must not acquire the scheduler lock in the opposite lock order.
            if self._group_yield.is_set():
                raise WorkBudgetExceeded('Shadow grouping yielded to serving work')
            return self._stop.is_set()

        try:
            def prepare():
                with cancellable_work(should_stop):
                    self.preparation.groups.step(self.ctx.now_fn())
                    if self.ctx.store.taste_groups.head() is None:
                        self.ctx.store.maintenance_diagnostics.record(
                            'deferred', phase=group_serving.PHASE, reasons=('group-head',))
                        return None
                    return group_serving.prepare(self.ctx.store, self.ctx.now_fn())

            with self.model_lock:
                preview = self._stage('adaptive grouping shadow', prepare)
            if preview is None:
                state.deferred(time.monotonic())
                if group_preparation.status(self.ctx.store).get('phase') == 'ready':
                    group_preparation.report(self.ctx.store, 'deferred', self.ctx.now_fn(), reason='preview-pending')
            else:
                state.succeeded()
                if preview and group_serving.enabled(self.ctx.store):
                    self._schedule(full=False, home=True)
                if group_preparation.status(self.ctx.store).get('phase') in {'failed', 'preparing'}:
                    group_preparation.report(self.ctx.store, 'ready', self.ctx.now_fn())
        except WorkClosing:
            raise
        except WorkBudgetExceeded:
            state.deferred(time.monotonic())
            self.ctx.store.maintenance_diagnostics.record(
                'deferred', phase=group_serving.PHASE, reasons=('serving-work',))
            group_preparation.report(self.ctx.store, 'deferred', self.ctx.now_fn(), reason='serving-work')
        except Exception:
            state.failed(time.monotonic(), base=60, cap=3600)
            group_preparation.report(self.ctx.store, 'failed', self.ctx.now_fn(), reason='preparation-failed')
            self.ctx.logger.warning('Adaptive grouping deferred; retaining previous shadow', exc_info=True)

    def _do_rebuild(self):
        # Source removal waits for publication, then clears every derived cache
        # before scheduling a build from the remaining evidence.
        with self.model_lock:
            self._rebuild()

    def _refresh_trends(self):
        return self.projections.run('trends')

    def _refresh_bundles(self):
        from yt_playlist.rec import mode_surfaces
        store = self.ctx.store
        # Model fitting can succeed while ranking fails. Keep the pool's retry
        # clock separate so it cannot stall ratings or reset its own backoff.
        with self._lock:
            state = self.jobs.retry[Job.BUNDLES]
            if time.monotonic() < state.retry_at:
                state.dirty = True
                return None
            state.dirty = False
            state.started(time.monotonic())
        try:
            result = self._stage("cluster surfaces and PPR", mode_surfaces.prepare_bundles,
                                 store, self.ctx.now_fn())
        except WorkClosing:
            raise
        except Exception:
            with self._lock:
                backoff = state.failed(time.monotonic(), base=5, cap=300, exponent_cap=6)
            self.ctx.logger.warning('rec bundle preparation failed: attempt=%d retry_backoff=%.1fs; '
                                    'retaining published pools', state.failures, backoff, exc_info=True)
            return None
        if result is None:
            self.ctx.logger.debug("rec mode bundles deferred: database changed during calculation")
            with self._lock:
                state.deferred(time.monotonic())
        else:
            with self._lock:
                state.succeeded()
            self.ctx.logger.debug("rec rebuild: mode bundles prepared")
            # Catch-up can publish a new pool every five seconds. Both partial
            # and drained generations share Home's automatic preparation budget.
            self.trigger_home_refresh()
            self._schedule(full=False, groups=True)
        return result

    def _rebuild(self):
        """Fit privately; keep the last completed model available throughout the work."""
        from yt_playlist.rec import model_refresh
        store = self.ctx.store
        with self._lock:
            state = self.jobs.retry[Job.FULL]
            if time.monotonic() < state.retry_at:
                state.dirty = True
                return
            state.started(time.monotonic())
        restore = store.settings.get_setting('rec_selection_restore')
        restored = False
        if restore:
            self._stage('model followup', model_selection.update_followup, store,
                        self.ctx.now_fn(), now_fn=self.ctx.now_fn)
            restored = store.model_selection.restore_model_selection(int(restore), self.ctx.now_fn())
            store.settings.delete_setting('rec_selection_restore')
        checkpoint = self.preparation.full_checkpoint(self.ctx.now_fn())
        def fit():
            try:
                with work_budget(20):
                    return model_refresh.rebuild(store, self.ctx.now_fn(), keep_collaborative=restored,
                                                memory_bytes=self.preparation.budget.full_memory_bytes,
                                                fit_cache=self._fit_cache)
            except model_refresh.MemoryBudgetExceeded:
                store.maintenance_diagnostics.record('deferred', reasons=('full-fit-memory-budget',))
                return None
            except WorkBudgetExceeded:
                store.maintenance_diagnostics.record('deferred', reasons=('full-fit-time-budget',))
                return None
        result = self._stage('recommendation models', fit)
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
        self.preparation.completed_full(checkpoint)
        try:
            self._stage('model selection', model_selection.maintain, store, self.ctx.now_fn(), now_fn=self.ctx.now_fn)
        except WorkClosing:
            raise
        except Exception:
            self.ctx.logger.warning('automatic model comparison failed; retaining published models', exc_info=True)
        self._refresh_bundles()
