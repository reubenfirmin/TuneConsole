"""The packaged builds have no terminal, so app.log is the only record they leave. These pin the
three things that make that true: the file gets written, repeat configure() calls don't stack
handlers on a stale fd (uvicorn --reload re-imports build_app), and a None stderr (the macOS
.app, built with console=False) doesn't take the process down."""
import logging
import logging.handlers
import sys

import pytest

from yt_playlist.core import logsetup


@pytest.fixture(autouse=True)
def _restore_logging():
    """configure() mutates the root logger and two excepthooks; put them back."""
    root = logging.getLogger()
    saved = list(root.handlers), root.level, sys.excepthook, threading_hook()
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved[0]:
        root.addHandler(h)
    root.setLevel(saved[1])
    sys.excepthook = saved[2]
    set_threading_hook(saved[3])
    logsetup._installed.clear()
    logsetup._hooks_installed = False


def threading_hook():
    import threading
    return threading.excepthook


def set_threading_hook(hook):
    import threading
    threading.excepthook = hook


def _file_handlers(root):
    return [h for h in root.handlers if isinstance(h, logging.handlers.TimedRotatingFileHandler)]


def test_configure_writes_records_to_the_log_file(tmp_path):
    path = tmp_path / "app.log"
    logsetup.configure(log_path=path)
    logging.getLogger("yt_playlist.test").error("YouTube request failed")
    for h in _file_handlers(logging.getLogger()):
        h.flush()
    assert "YouTube request failed" in path.read_text()


def test_configure_creates_missing_log_directory(tmp_path):
    path = tmp_path / "logs" / "app.log"
    logsetup.configure(log_path=path)
    assert path.parent.is_dir()


def test_configure_is_idempotent(tmp_path):
    """--reload re-imports build_app() in a child; handlers must not accumulate."""
    logsetup.configure(log_path=tmp_path / "app.log")
    logsetup.configure(log_path=tmp_path / "app.log")
    assert len(_file_handlers(logging.getLogger())) == 1


def test_configure_leaves_foreign_handlers_alone(tmp_path):
    """We retire only what we installed, so pytest's caplog and friends survive."""
    root = logging.getLogger()
    foreign = logging.NullHandler()
    root.addHandler(foreign)
    logsetup.configure(log_path=tmp_path / "app.log")
    logsetup.configure(log_path=tmp_path / "app.log")
    assert foreign in root.handlers


def test_configure_survives_a_none_stderr(tmp_path, monkeypatch):
    """PyInstaller's console=False build really does set sys.stderr to None."""
    monkeypatch.setattr(sys, "stderr", None)
    logsetup.configure(log_path=tmp_path / "app.log")
    root = logging.getLogger()
    assert not [h for h in root.handlers if type(h) is logging.StreamHandler]
    logging.getLogger("yt_playlist.test").info("still alive")   # must not raise


def test_configure_adds_stderr_handler_when_present(tmp_path):
    logsetup.configure(log_path=tmp_path / "app.log")
    root = logging.getLogger()
    assert [h for h in root.handlers if type(h) is logging.StreamHandler]


def test_log_level_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_PLAYLIST_LOG_LEVEL", "debug")
    logsetup.configure(log_path=tmp_path / "app.log")
    assert logging.getLogger().level == logging.DEBUG


def test_log_level_defaults_to_info_on_garbage(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_PLAYLIST_LOG_LEVEL", "not-a-level")
    logsetup.configure(log_path=tmp_path / "app.log")
    assert logging.getLogger().level == logging.INFO


def test_uncaught_exception_is_logged_and_delegated(tmp_path):
    """The whole point: a crash in a packaged build leaves a traceback behind."""
    called = []
    sys.excepthook = lambda *a: called.append(a)
    logsetup.configure(log_path=tmp_path / "app.log")
    try:
        raise ValueError("boom")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    for h in _file_handlers(logging.getLogger()):
        h.flush()
    text = (tmp_path / "app.log").read_text()
    assert "uncaught exception" in text
    assert "ValueError: boom" in text
    assert called, "the original excepthook must still run"


def test_keyboard_interrupt_is_not_logged_as_a_crash(tmp_path):
    called = []
    sys.excepthook = lambda *a: called.append(a)
    logsetup.configure(log_path=tmp_path / "app.log")
    try:
        raise KeyboardInterrupt
    except KeyboardInterrupt:
        sys.excepthook(*sys.exc_info())
    for h in _file_handlers(logging.getLogger()):
        h.flush()
    assert "uncaught exception" not in (tmp_path / "app.log").read_text()
    assert called, "Ctrl-C must still reach the original excepthook"


def test_uncaught_thread_exception_is_logged(tmp_path):
    import threading
    logsetup.configure(log_path=tmp_path / "app.log")

    def explode():
        raise RuntimeError("thread boom")

    t = threading.Thread(target=explode, name="exploder")
    t.start()
    t.join()
    for h in _file_handlers(logging.getLogger()):
        h.flush()
    text = (tmp_path / "app.log").read_text()
    assert "uncaught exception in thread exploder" in text
    assert "RuntimeError: thread boom" in text
