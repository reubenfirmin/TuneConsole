"""Opt-in, flushed test receipts for serial runs and local xdist coordinators."""
from datetime import datetime, timezone
from json import dumps as _dumps
from pathlib import Path
from time import monotonic as _monotonic

import pytest

from ci_test_shards import _diagnostic


def pytest_addoption(parser):
    parser.getgroup("test diagnostics").addoption(
        "--test-progress", type=Path, metavar="PATH",
        help="Print live test starts/phase outcomes, including bounded failure details, and append timestamped JSONL receipts to PATH.",
    )


def pytest_configure(config):
    path = config.getoption("test_progress")
    if path is None:
        return
    config.pluginmanager.register(InterruptedCoverage(config), "interrupted-test-coverage")
    if hasattr(config, "workerinput"):
        return
    # Only the coordinator writes. Workers forward starts and reports through
    # xdist, including setup/teardown failures, without disabling capture.
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("w", encoding="utf-8", buffering=1)
    config.add_cleanup(stream.close)
    config.pluginmanager.register(Progress(config, stream), "live-test-progress")


class InterruptedCoverage:
    def __init__(self, config):
        self.config = config

    def pytest_runtest_logfinish(self, nodeid, location):
        if not hasattr(self.config, "workerinput"):
            return
        plugin = self.config.pluginmanager.getplugin("_cov")
        controller = getattr(plugin, "cov_controller", None)
        if controller is not None and controller.started:
            # xdist can close a worker transport on Ctrl-C before the active
            # call unwinds. Checkpoint completed tests in diagnostic runs so
            # their per-test contexts survive even that shutdown path.
            controller.cov.save()

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self, session, exitstatus):
        # pytest-cov 7.1 finishes in its runtestloop wrapper after `yield`.
        # KeyboardInterrupt skips that code. Use the same controller finish
        # path, including worker save/merge, only when it is still running.
        def save():
            plugin = session.config.pluginmanager.getplugin("_cov")
            controller = getattr(plugin, "cov_controller", None)
            # --maxfail can also unwind runtestloop before pytest-cov combines
            # its suffix file. Interrupted masters remerge after workers drain.
            if controller is not None and (controller.started or (
                    not hasattr(session.config, "workerinput") and exitstatus in (
                        pytest.ExitCode.INTERRUPTED, pytest.ExitCode.INTERNAL_ERROR))):
                controller.finish()
        worker = hasattr(session.config, "workerinput")
        if worker:
            save()  # Before xdist sends its final workeroutput.
        try:
            return (yield)
        finally:
            if not worker:
                save()  # After xdist has drained/joined all workers.


class Progress:
    def __init__(self, config, stream):
        self.config, self.stream = config, stream
        self.started = _monotonic()
        self.collected = False
        self.emitting = False

    def emit(self, event, **values):
        # The runtime monitor uses SIGALRM. Let it defer a tick while a
        # receipt or its terminal output holds a non-reentrant I/O lock.
        self.emitting = True
        try:
            self._emit(event, **values)
        finally:
            self.emitting = False

    def _emit(self, event, **values):
        row = {"event": event, "utc": datetime.now(timezone.utc).isoformat(),
               "elapsed_seconds": _monotonic() - self.started, **values}
        self.stream.write(_dumps(row, ensure_ascii=False) + "\n")
        self.stream.flush()
        terminal = self.config.pluginmanager.getplugin("terminalreporter")
        if terminal and event != "end":
            detail = f" {values['phase']} {values['outcome']} {values['seconds']:.3f}s" if event == "phase" else ""
            if event == "finish":
                detail = f" exitstatus={values['exitstatus']}"
            if event == "collection":
                detail = f" tests={values['tests']} files={values['files']}"
            if event == "pace_stop":
                detail = " " + values['message']
            terminal.write_line(f"[{row['utc']}] {event} {values.get('nodeid', '')}{detail}")
            if event == "phase" and values['outcome'] == "failed":
                terminal.write_line(values['diagnostic'])
                if values['captured']:
                    terminal.write_line(values['captured'])
            terminal.flush()

    def collection(self, ids):
        if not self.collected:
            self.collected = True
            self.emit("collection", tests=len(ids), files=len({nodeid.split("::", 1)[0] for nodeid in ids}),
                      nodeids=ids)

    def pytest_collection_finish(self, session):
        if not self.config.getoption("numprocesses", default=0):
            self.collection([item.nodeid for item in session.items])

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        self.collection(ids)

    def pytest_runtest_logstart(self, nodeid, location):
        self.emit("start", nodeid=nodeid)

    def pytest_runtest_logreport(self, report):
        details = {}
        if report.failed:
            # Keep the traceback head and exception tail within the same JSON
            # byte limit as CI receipts, with an explicit truncation marker.
            # Leave the report intact for pytest's complete final artifacts.
            details['diagnostic'] = _diagnostic(report.longreprtext)
            details['captured'] = _diagnostic('\n'.join(f'{name}\n{text}' for name, text in report.sections))
        self.emit("phase", nodeid=report.nodeid, phase=report.when,
                  outcome=report.outcome, seconds=report.duration,
                  worker=getattr(report, "worker_id", "serial"), **details)

    def pytest_runtest_logfinish(self, nodeid, location):
        self.emit("end", nodeid=nodeid)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        self.emit("finish", exitstatus=int(exitstatus))
