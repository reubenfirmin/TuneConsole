"""Thread-safe bridge between the browser server and Sparkle's main-thread UI.

Cocoa owns the main thread; uvicorn runs on a worker thread. Native calls are
queued here and consumed by the Cocoa timer, never made from an HTTP request.
"""
import queue
import threading
import time
import uuid

from yt_playlist.core.update_lifecycle import acquire_restart_locks

RESUME_KEY = "TuneConsoleUpdateResume"


def launch_arguments(argv, resume=None, *, no_open=False, now=None):
    """An update relaunch restores its address and reconnects the existing tab."""
    argv = list(argv)
    now = time.time() if now is None else now
    try:
        saved = list(resume["argv"]) if resume else []
        resumed = (not argv and saved and all(isinstance(arg, str) for arg in saved)
                   and 0 <= now - float(resume["time"]) < 600)
    except (KeyError, TypeError, ValueError):
        resumed = False
    if resumed:
        argv = saved
        no_open = True
        if "--wait-for-port" not in argv:
            argv.append("--wait-for-port")
    if not no_open and "--open" not in argv:
        argv.insert(0, "--open")
    if "--exit-on-idle" not in argv:
        argv.insert(0, "--exit-on-idle")
    return argv


class MacOSUpdater:
    def __init__(self):
        self.instance = uuid.uuid4().hex
        self.phase = "available"
        self.message = ""
        self.commands = queue.Queue()
        self.server = None
        self._lock = threading.RLock()
        self._quitting = False
        self._held_locks = None

    @property
    def busy(self):
        with self._lock:
            return self._quitting or self.phase in ("installing", "restarting")

    def set_state(self, phase, message=""):
        with self._lock:
            if not self._quitting:
                self.phase, self.message = phase, message

    def snapshot(self):
        with self._lock:
            return {"instance": self.instance, "phase": self.phase,
                    "message": self.message, "progress": None}

    async def status(self):
        return self.snapshot()

    async def install(self):
        with self._lock:
            if not self.busy and self.phase != "unavailable":
                self.phase = "installing"
                self.message = "Continue in the macOS update window."
                self.commands.put("check")
        return self.snapshot()

    async def restart(self, host, port):
        raise ValueError("Use Install and Relaunch in the macOS update window.")

    def attach(self, server):
        self.server = server

    def request_quit(self):
        with self._lock:
            self._quitting = True

    def poll_quit(self):
        """Drain active work before Cocoa terminates or Sparkle replaces the bundle."""
        with self._lock:
            if not self._quitting or self.server is None or self._held_locks is not None:
                return
            # Cocoa owns this quit, including Sparkle's install-and-relaunch handoff.
            # Our own busy flag must not prevent the server's work from draining.
            locks, message = acquire_restart_locks(
                self.server.config.app.state.ctx, wait_for_update=False)
            if locks is None:
                self.message = message
                return
            self._held_locks = locks  # held until process exit, including throughout server shutdown
            self.phase, self.message = "restarting", "Closing TuneConsole…"
            self.server.should_exit = True

    async def close(self):
        # Sparkle and its installer belong to Cocoa; closing ASGI must not cancel installation.
        pass
