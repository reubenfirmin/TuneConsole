"""Application logging, configured so the packaged builds are not silent.

Neither packaged target has usable stdio: the Flatpak launched from the desktop gets
stdout/stderr on /dev/null, and the macOS .app is built with `console=False`, which leaves
`sys.stderr` as None. A crash or a failed YouTube call in either therefore left no trace at
all. So the file is the primary sink and the console is the optional one, not the reverse.

Deliberately shaped like `EgressGuard._configure_logger`: same rotation, and the same
reset-rather-than-accumulate discipline, so `uvicorn --reload` re-importing `build_app()`
cannot stack handlers on a stale file descriptor.
"""
import logging
import logging.handlers
import os
import sys
import threading

from yt_playlist.core import paths

LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"

logger = logging.getLogger(__name__)

# Handlers this module installed, so a second configure() can retire exactly those and leave
# any handler someone else attached (pytest's caplog, for instance) alone.
_installed: list[logging.Handler] = []
_hooks_installed = False


def _level() -> int:
    """INFO, unless YT_PLAYLIST_LOG_LEVEL names another level. Lets a packaged build be turned
    up to DEBUG without a rebuild, which is the only lever a user of the .app or Flatpak has."""
    name = os.environ.get("YT_PLAYLIST_LOG_LEVEL", "").strip().upper()
    return getattr(logging, name, logging.INFO) if name else logging.INFO


def _install_excepthooks() -> None:
    """Route uncaught exceptions into the log, then hand off to the original hook so a terminal
    run still prints its traceback the way Python would have."""
    global _hooks_installed
    if _hooks_installed:
        return
    prev_sys, prev_thread = sys.excepthook, threading.excepthook

    def _on_uncaught(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):     # Ctrl-C is a request, not a fault
            logger.critical("uncaught exception", exc_info=(exc_type, exc, tb))
        prev_sys(exc_type, exc, tb)

    def _on_thread_uncaught(args):
        if not issubclass(args.exc_type, SystemExit):
            logger.critical("uncaught exception in thread %s", args.thread and args.thread.name,
                            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        prev_thread(args)

    sys.excepthook = _on_uncaught
    threading.excepthook = _on_thread_uncaught
    _hooks_installed = True


def configure(log_path=None) -> None:
    """Send the root logger to a rotating app.log, and to stderr when there is one.

    Root, not a named logger, because uvicorn logs a request's traceback under `uvicorn.error`
    and that is exactly the output we are trying to stop losing. Idempotent: safe to call from
    both `main()` and `build_app()`, and again in the `--reload` child.
    """
    root = logging.getLogger()
    for h in _installed:
        root.removeHandler(h)
        h.close()
    _installed.clear()

    path = log_path or paths.app_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.TimedRotatingFileHandler(
        path, when="midnight", backupCount=7, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)
    _installed.append(handler)

    # `console=False` in the PyInstaller spec means sys.stderr is None inside the .app; a
    # StreamHandler built on that raises on first emit.
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(stream)
        _installed.append(stream)

    root.setLevel(_level())
    _install_excepthooks()
