"""Serial retention slices with independent progress, failures and retry clocks."""
from dataclasses import dataclass

from yt_playlist.core.activity import WorkClosing

RETRY_SECONDS = 5
IDLE_SECONDS = 300


@dataclass
class Progress:
    result: object = None
    pending: bool = False
    failures: int = 0
    error: str | None = None
    retry_at: float = 0


class Retention:
    def __init__(self, store, now_fn, logger, *, execute, monotonic):
        # Construction does no collection or file/database inspection.
        self.store, self.now_fn, self.logger = store, now_fn, logger
        self.execute, self.monotonic = execute, monotonic
        self.progress = {}

    def run(self):
        from yt_playlist.rec import group_roots, measurement

        now = self.now_fn()
        repository = self.store.retention
        self._step('managed file lifecycle', repository.maintain_managed_files, now)
        self._step('prediction provenance retirement', repository.maintain_prediction_provenance, now)
        self._step('musical profile file collection', repository.collect_musical_profiles, now)
        self._step('embedding file collection', repository.collect_embeddings, now)
        self._step('legacy diagnostic expiry', repository.expire_legacy_diagnostics, now)
        self._step('runtime history retirement', repository.maintain_runtime_history, now,
                   pending=lambda result: bool(result and any(result.values())))
        retired = self._step('derived history retirement', group_roots.maintain_retention, self.store,
                             pending=lambda result: bool(result and result['pending']))
        if retired and any(retired.get(key, 0) for key in ('pools', 'generations', 'group_steps')):
            # Releasing owners creates eligible policies even when the previous
            # policy scan was idle. Do not leave that capacity asleep for 5 min.
            policies = self.progress.get('unused recommendation policies')
            if policies is not None and not policies.failures:
                policies.retry_at = self.monotonic()
        self._step('unused recommendation policies', measurement.maintain_storage, self.store, now,
                   progress=True, pending=lambda result: result['pending'])
        return min(state.retry_at for state in self.progress.values())

    def _step(self, name, target, *args, pending=None, **kwargs):
        state = self.progress.setdefault(name, Progress())
        if self.monotonic() < state.retry_at:
            return
        try:
            result = self.execute(name, target, *args, **kwargs)
            more = bool(pending and pending(result))
        except WorkClosing:
            # Shutdown cancels the bundle, rather than becoming an owner retry.
            raise
        except Exception as exc:
            state.failures += 1
            state.error = f'{type(exc).__name__}: {exc}'
            state.pending = True
            delay = min(IDLE_SECONDS, RETRY_SECONDS * 2 ** min(state.failures - 1, 6))
            state.retry_at = self.monotonic() + delay
            self.logger.warning('%s failed; retrying in %ss', name, delay, exc_info=True)
        else:
            state.result, state.pending = result, more
            state.failures, state.error = 0, None
            state.retry_at = self.monotonic() + (RETRY_SECONDS if more else IDLE_SECONDS)
            self.logger.debug('%s: %s', name, result)
            return result
