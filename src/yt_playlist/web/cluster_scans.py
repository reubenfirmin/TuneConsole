"""One managed, cancellable Clusters scan at a time per application."""
import asyncio
import threading

from yt_playlist.core.activity import activity_for, cancellable_work, check_stopping
from yt_playlist.core.numerics import limit_native_threads


class ClusterScans:
    def __init__(self, ctx):
        self.ctx = ctx
        self.gate = asyncio.Semaphore(1)

    async def run(self, calculate, body, publish=None):
        # Wait without occupying executor threads or allocating another matrix.
        await self.gate.acquire()
        cancelled = threading.Event()
        task = asyncio.create_task(activity_for(self.ctx).to_thread(
            self._calculate, calculate, body, publish, cancelled))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            # A cancelled HTTP request can leave its thread unwinding. Keep the
            # slot until that admitted work has actually released its reader.
            def finished(done):
                self.gate.release()
                if not done.cancelled():
                    done.exception()  # observe cancellation/shutdown exceptions
            task.add_done_callback(finished)

    def _calculate(self, calculate, body, publish, cancelled):
        limit_native_threads()
        with cancellable_work(cancelled.is_set):
            with self.ctx.store.read_snapshot() as snapshot:
                payload, details = calculate(snapshot.store, body)
                check_stopping()
            if publish is not None:
                check_stopping()
                publish(payload, details, body)
            return payload
