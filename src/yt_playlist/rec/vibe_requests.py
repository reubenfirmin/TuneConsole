"""Accepted Vibe selections, built independently of the HTTP request lifetime."""
import json
import threading
import time
from uuid import uuid4

from yt_playlist.core.activity import activity_for, cancellable_work, check_cancellable_work, WorkClosing
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.rec import home_cache, home_cards, home_prepared, home_vibe, mix_tuning


class VibeRequests:
    """One active calculation and the latest durable selection per account."""

    def __init__(self, ctx):
        self.ctx = ctx
        self._lock = threading.Lock()
        self._running = False
        self._retry_at = 0.

    def state(self, store=None):
        raw = (self.ctx.store if store is None else store).settings.get_setting(mix_tuning.VIBE_REQUEST_SETTING)
        return json.loads(raw) if raw else None

    def _save(self, state):
        self.ctx.store.settings.set_setting(mix_tuning.VIBE_REQUEST_SETTING, json.dumps(state))

    def select(self, vibe, now):
        if vibe and vibe not in home_vibe.PRESETS:
            raise ValueError('Unknown Vibe')
        request = {'id': uuid4().hex, 'vibe': vibe, 'phase': 'pending'}
        store = self.ctx.store
        with mix_tuning.adjusting_vibe(store, now):
            if vibe:
                store.settings.set_setting(home_vibe.SETTING, vibe)
            else:
                store.settings.delete_setting(home_vibe.SETTING)
            self._save(request)
        self.resume()
        return request

    def resume(self):
        # Starting accepted work also works when periodic maintenance is off.
        # The application activity owner drains this thread during shutdown.
        with self._lock:
            if self._running:
                return
            request = self.state()
            if not request or request['phase'] not in ('pending', 'waiting'):
                return
            if request['phase'] == 'waiting' and time.monotonic() < self._retry_at:
                return
            self._running = True
            try:
                activity_for(self.ctx).start_thread(self._run, name='home-vibe-generation')
            except BaseException:
                self._running = False
                raise

    def _finish(self, request, phase):
        # Called inside the same transaction as the cache-head switch. Only
        # SQL settings checks belong here; all ranking and file I/O is finished.
        store = self.ctx.store
        current = self.state()
        if not current or current['id'] != request['id']:
            return False
        if (store.settings.get_setting(home_vibe.SETTING) or '') != request['vibe']:
            return False
        self._save({**request, 'phase': phase})
        return True

    def _build(self, request):
        store, now = self.ctx.store, self.ctx.now_fn()
        mix_tuning.expire(store, now)
        # No overall generation deadline. Each database operation is observable
        # through the existing SQL diagnostics; CPU/file work is off the writer.
        with store.read_snapshot() as snapshot:
            reader = snapshot.store
            with home_vibe.authorized(reader, request['vibe']), home_cache.verified_reads(reader):
                if self.state(reader) != request:
                    return
                payload = home_prepared.selection_context(reader, now)
                previous = home_prepared.peek(reader, now, readiness_only=True)
                previous_size = max((len(menu['protos']) for menu in (previous or {}).get('menus', [])), default=0)
                calculation = home_cards.HomeCards(reader, home_prepared._NoJobs(), self.ctx.logger,
                    genre_options=(payload or {}).get('genre_options'))
                result = calculation.cards(home_cards.CardsRequest(now, record=False))
                tuning = home_prepared.tuning_key(reader, now)
            snapshot.release_reader()
            check_cancellable_work()
            phase = 'ready' if len(result.protos) >= 4 and not result.preparing else 'waiting'
            self.ctx.logger.info('Vibe result: request=%s preset=%s phase=%s lanes=%s requested_genres=%s',
                request['id'], request['vibe'], phase, [p['lane'] for p in result.protos], result.requested_genres)
            # A partial preparation must not replace an existing complete board.
            if not result.protos or (phase == 'waiting' and len(result.protos) < previous_size):
                with atomic_write(store):
                    self._finish(request, phase)
            else:
                home_prepared.remember(store, payload, result, now, tuning=tuning,
                    before_publish=lambda: snapshot.is_current() and self._finish(request, phase))
        if phase == 'waiting' and self.ctx.rec_worker:
            self.ctx.rec_worker.trigger_candidate_refresh()

    def _run(self):
        request = None
        try:
            with cancellable_work(lambda: False), self.ctx.store.priority.background(check_cancellable_work):
                while True:
                    request = self.state()
                    if not request or request['phase'] not in ('pending', 'waiting'):
                        return
                    started, cpu = time.monotonic(), time.thread_time()
                    try:
                        self._build(request)
                    except WorkClosing:
                        # Its persisted intent is resumed after restart.
                        return
                    except Exception:
                        check_cancellable_work()
                        self.ctx.logger.exception('Vibe generation failed: request=%s preset=%s',
                                                  request['id'], request['vibe'])
                        with atomic_write(self.ctx.store):
                            self._finish(request, 'failed')
                    finally:
                        self.ctx.logger.info('Vibe generation: request=%s preset=%s wall=%.3fs cpu=%.3fs',
                            request['id'], request['vibe'], time.monotonic() - started, time.thread_time() - cpu)
                    with self._lock:
                        current = self.state()
                        if current and current['id'] != request['id'] and current['phase'] == 'pending':
                            continue
                        self._retry_at = time.monotonic() + 3
                        return
        except WorkClosing:
            return
        finally:
            with self._lock:
                self._running = False
            # A selection can commit between the loop's last check and exit.
            # Rejoin that durable latest request without creating parallel builds.
            if activity_for(self.ctx).accepting:
                current = self.state()
                if current and request and current['id'] != request['id']:
                    self.resume()
