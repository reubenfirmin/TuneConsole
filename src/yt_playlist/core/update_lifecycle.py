"""Drain admitted work and own desktop restart requests until their handoff."""
import asyncio

from yt_playlist.core.activity import activity_for


def acquire_restart_locks(ctx, *, reason="restart", wait_for_update=True):
    work = activity_for(ctx)
    work.close_admission(reason)
    if work.closing_reason != reason:
        return None, "TuneConsole is already closing…"
    updater = getattr(ctx, "updater", None)
    if wait_for_update and updater is not None and updater.busy:
        return None, "Waiting for the update to finish…"
    if work.active:
        return None, "Waiting for background work to finish…"
    acquired = []
    try:
        for lock in (ctx.sync_lock, ctx.spotify_import_lock):
            if not lock.acquire(blocking=False):
                for held in acquired:
                    held.release()
                return None, "Waiting for your sync or import to finish…"
            acquired.append(lock)
        if ctx.jobs.has_active():
            for held in acquired:
                held.release()
            return None, "Waiting for background work to finish…"
    except BaseException:
        for held in acquired:
            held.release()
        raise
    return acquired, ""


class PendingRestart:
    """One server task per restart, independent of the requesting connection.

    Cancellation is allowed only while draining. Once launch begins, the remote
    spawn must finish before locks can be released or another attempt accepted.
    """

    def __init__(self, ctx, stop_server, *, poll_interval=0.2):
        self.ctx = ctx
        self.stop_server = stop_server
        self.poll_interval = poll_interval
        self.task = None
        self.phase = None
        self.message = ""

    def snapshot(self, state):
        if self.phase:
            state = {**state, "phase": self.phase}
        return {**state, "message": self.message or state.get("message", ""),
                "busy": self.phase in ("draining", "launching"),
                "can_cancel": self.phase == "draining"}

    def request(self, host, port):
        if self.phase is not None:
            return
        work = activity_for(self.ctx)
        work.close_admission("restart")
        if work.closing_reason != "restart":
            raise ValueError("TuneConsole is already closing.")
        self.phase, self.message = "draining", "Waiting for background work to finish…"
        # Do not register the coordinator as admitted work: it would wait on itself.
        self.task = asyncio.create_task(self._run(host, port), name="pending-app-restart")

    async def _run(self, host, port):
        acquired = []
        launched = False
        work = activity_for(self.ctx)
        try:
            while work.closing_reason == "restart":
                if self.ctx.updater.phase != "ready":
                    raise ValueError("The installed update is no longer ready to restart.")
                acquired, self.message = acquire_restart_locks(self.ctx)
                if acquired is not None:
                    break
                await asyncio.sleep(self.poll_interval)
            else:
                return
            self.phase, self.message = "launching", "Starting the updated TuneConsole…"
            result = await self.ctx.updater.restart(host, port)
            if result.get("phase") != "restarting":
                self.message = result.get("message", "")
                return
            launched = True
            self.phase, self.message = "restarting", "Restarting TuneConsole…"
            # Hold locks through process exit. No request or browser owns this handoff.
            try:
                await self.stop_server()
            except Exception:
                # A replacement was already spawned; never spawn a duplicate on retry.
                self.ctx.updater.phase = "manual_restart"
                launched = False
                self.message = "Close TuneConsole, then open it again to finish the update."
                self.ctx.logger.warning("could not stop TuneConsole after update launch", exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.message = "Couldn't restart TuneConsole. Please try again."
            self.ctx.logger.warning("could not launch updated TuneConsole", exc_info=True)
        finally:
            if not launched:
                for held in acquired or ():
                    held.release()
                work.resume_restart()
                self.phase = None

    async def cancel(self):
        if self.phase != "draining":
            return False
        self.message = "Restart cancelled. You can restart when you're ready."
        task = self.task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # A task cancelled before its first turn has not entered its finally block.
        # Another tab may already have requested a new restart after cleanup.
        if self.task is task:
            activity_for(self.ctx).resume_restart()
            self.phase = None
        return True

    async def close(self):
        if self.phase == "draining":
            await self.cancel()
        elif self.task is not None and not self.task.done():
            await asyncio.shield(self.task)
