"""Schedule local observations without stopping the transport's response pump."""
from copy import deepcopy
from threading import Lock


class BridgeObservations:
    def __init__(self, work, *, observe, connected, prepare, logger, suspend=lambda: None):
        self.work, self.observe = work, observe
        self.connected, self.prepare, self.logger = connected, prepare, logger
        self.suspend = suspend
        self._pending, self._lock = set(), Lock()

    def _submit(self, target, *args):
        future = self.work.submit(target, *args)
        with self._lock:
            self._pending.add(future)
        def finished(result):
            try:
                result.result()
            except Exception:
                self.logger.warning('bridge observation remains unconfirmed', exc_info=True)
            finally:
                with self._lock:
                    self._pending.discard(result)
        future.add_done_callback(finished)
        return future

    def drain(self):
        with self._lock:
            pending = tuple(self._pending)
        for future in pending:
            try:
                future.result()
            except Exception:
                pass  # The completion callback owns diagnostics.

    def connection(self):
        return self._submit(self.connected)

    def retire(self):
        """Finish this socket's admitted observations before suspending its session."""
        self.drain()
        self.suspend()

    def receive(self, kind, body, now):
        return self._submit(self.observe, kind, deepcopy(body), now)

    def prepare_radio(self, playing, body):
        if (not playing.get('video_id') or not playing.get('playlist_ytm_id') or playing.get('paused')
                or body.get('muted') or body.get('tabMuted') or body.get('volume') == 0
                or body.get('metadataVerified') is False):
            return
        try:
            self.prepare()
        except Exception:
            self.logger.debug('Radio reattachment deferred', exc_info=True)
