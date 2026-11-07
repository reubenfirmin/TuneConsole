"""Bound focused diagnostics or stop a suite that falls behind its runtime target."""
import asyncio
from contextlib import contextmanager
import faulthandler
import json
import math
import os
from pathlib import Path
import signal
import sys
import traceback
from time import monotonic as _monotonic

import pytest


def coverage_data_locked():
    # An asynchronous exception between coverage's acquire/release callbacks
    # strands its non-reentrant lock. Rollback/teardown then traces into that
    # same lock and cannot finish. Defer this timer tick until tracing is idle.
    module = sys.modules.get('coverage')
    coverage = module.Coverage.current() if module is not None else None
    collector = getattr(coverage, '_collector', None)
    lock = getattr(collector, 'data_lock', None)
    return lock is not None and lock.locked()


def progress_emitting(config):
    progress = config.pluginmanager.getplugin("live-test-progress")
    return progress is not None and progress.emitting


def pytest_addoption(parser):
    group = parser.getgroup("test diagnostics")
    group.addoption(
        "--test-time-limit", type=float, default=0, metavar="SECONDS",
        help="Interrupt this session after SECONDS, preserving teardown and reports (0 disables).",
    )
    group.addoption(
        "--test-runtime-target", type=float, default=0, metavar="SECONDS",
        help="Stop when completed-test progress falls behind this whole-suite target (0 disables).",
    )
    group.addoption(
        "--test-runtime-slack", type=float, default=None, metavar="SECONDS",
        help="Allowed delay behind the runtime target line (default: 10%% of the target).",
    )


def pytest_configure(config):
    seconds = config.getoption("test_time_limit")
    target = config.getoption("test_runtime_target")
    slack = config.getoption("test_runtime_slack")
    for option, value in (("test-time-limit", seconds), ("test-runtime-target", target),
                          ("test-runtime-slack", slack)):
        if value is not None and (not math.isfinite(value) or value < 0):
            raise pytest.UsageError(f"--{option} must be a finite, nonnegative number")
    if seconds and target:
        raise pytest.UsageError("Choose --test-time-limit or --test-runtime-target")
    if slack is not None and not target:
        raise pytest.UsageError("--test-runtime-slack requires --test-runtime-target")
    if not (seconds or target):
        return
    if not hasattr(signal, "setitimer"):
        raise pytest.UsageError("Test runtime monitoring requires POSIX interval timers")
    limit = RuntimeTarget(config, target, slack) if target else SessionLimit(config, seconds)
    config.pluginmanager.register(limit, "test-session-time-limit")
    config.add_cleanup(limit.cleanup)


class SessionLimit:
    def __init__(self, config, seconds):
        self.config, self.seconds = config, seconds
        self.deadline = None
        self.previous_handler = None
        self.interruption = None
        self.browser_calls = {}
        self.tearing_down = False
        self.delivered = False
        self.browser_patch = None

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self, session):
        self.deadline = getattr(self.config, "workerinput", {}).get(
            "test_time_limit_deadline", _monotonic() + self.seconds)
        self.start_timer(max(.001, self.deadline - _monotonic()))

    def start_timer(self, delay, interval=0):
        if any(signal.getitimer(signal.ITIMER_REAL)):
            raise pytest.UsageError("Test runtime monitoring cannot replace an existing interval timer")
        self.previous_handler = signal.signal(signal.SIGALRM, self.expired)
        self.protect_browser_transport()
        signal.setitimer(signal.ITIMER_REAL, delay, interval)

    def protect_browser_transport(self):
        """Cancel awaitables, then interrupt in the synchronous caller's fiber.

        Throwing from SIGALRM while the dispatcher is running destroys its
        greenlet. Task/Future cancellation instead follows Playwright's normal
        completion callback back to the caller, leaving tracing/close usable.
        EventInfo waits use the same dispatcher without going through _sync.
        Keep these adapters scoped to sessions that requested a runtime monitor.
        """
        if self.browser_patch is not None:
            return
        from playwright._impl._sync_base import EventInfo, SyncBase
        from playwright.sync_api._context_manager import PlaywrightContextManager

        patch = self.browser_patch = pytest.MonkeyPatch()
        original_sync, original_value = SyncBase._sync, EventInfo.value.fget
        original_enter = PlaywrightContextManager.__enter__
        original_exit = PlaywrightContextManager.__exit__
        monitor = self

        def sync(owner, coro):
            task = None

            async def command():
                nonlocal task
                task = asyncio.current_task()
                try:
                    if monitor.interruption and not monitor.delivered and not monitor.tearing_down:
                        raise asyncio.CancelledError
                    return await coro
                finally:
                    coro.close()

            def cancel():
                if task is not None:
                    task.cancel()

            with monitor.browser_boundary(owner._loop, cancel, coro.__qualname__, owner._dispatcher_fiber):
                return original_sync(owner, command())

        def value(event):
            with monitor.browser_boundary(event._sync_base._loop, event._future.cancel, 'EventInfo.value',
                                          event._sync_base._dispatcher_fiber):
                return original_value(event)

        def enter(owner):
            # Driver startup also switches directly into the dispatcher. Let
            # it return normally, then close the acquired driver if setup was
            # stopped before pytest could register the fixture's finalizer.
            with monitor.browser_boundary(None, None, 'Playwright.start'):
                result = original_enter(owner)
                if monitor.interruption and not monitor.delivered:
                    original_exit(owner)
                return result

        def exit(owner, *args):
            with monitor.browser_boundary(None, None, 'Playwright.stop'):
                return original_exit(owner, *args)

        patch.setattr(SyncBase, '_sync', sync)
        patch.setattr(EventInfo, 'value', property(value))
        patch.setattr(PlaywrightContextManager, '__enter__', enter)
        patch.setattr(PlaywrightContextManager, '__exit__', exit)

    @contextmanager
    def browser_boundary(self, loop, cancel, operation, dispatcher=None):
        from greenlet import getcurrent

        boundary = object()
        self.browser_calls[boundary] = (loop, cancel, operation, getcurrent(), dispatcher)
        try:
            yield
        finally:
            # Route/event handlers own independent greenlets. Their calls may
            # remain suspended after the ordinary caller's command completes.
            self.browser_calls.pop(boundary)
            if (self.interruption and not self.delivered and not self.tearing_down
                    and self.active_browser_call() is None):
                self.delivered = True
                raise KeyboardInterrupt(self.interruption) from None

    def active_browser_call(self):
        from greenlet import getcurrent

        current = getcurrent()
        lineage = set()
        ancestor = current
        while ancestor is not None:
            lineage.add(ancestor)
            ancestor = ancestor.parent
        for call in self.browser_calls.values():
            _, _, _, caller, dispatcher = call
            # A suspended callback is a sibling of an ordinary caller, not
            # its active browser wait. During dispatch, prefer the enclosing
            # caller on the running fiber's ancestry over suspended callbacks.
            if caller in lineage and (caller is current or dispatcher is None or dispatcher in lineage):
                return call
        return None

    def interrupt(self):
        signal.setitimer(signal.ITIMER_REAL, 0)
        if self.tearing_down:
            # Do not abandon a popped fixture finalizer, including tracing.stop
            # and admitted HTTP/native publication. Report the stop afterwards.
            return
        call = self.active_browser_call()
        if call is not None:
            loop, cancel, operation, caller, _ = call
            try:
                # faulthandler sees the running dispatcher fiber. Retain the
                # suspended synchronous caller as well, without browser calls
                # or reentering a captured Python stream's I/O lock.
                diagnostic = (f'Browser stop: {operation}: {self.interruption}\n'
                              + ''.join(traceback.format_stack(caller.gr_frame)))
                capture = self.config.pluginmanager.getplugin('capturemanager')
                error_capture = getattr(getattr(capture, '_global_capturing', None), 'err', None)
                descriptor = getattr(error_capture, 'targetfd_save', None)
                if descriptor is None:
                    descriptor = sys.__stderr__.fileno()
                os.write(descriptor, diagnostic.encode('utf-8', errors='replace'))
                faulthandler.dump_traceback(file=descriptor, all_threads=True)
                progress = self.config.pluginmanager.getplugin('live-test-progress')
                if progress:
                    progress.emit('browser_stop', message=self.interruption, operation=operation)
            except Exception as error:
                # A diagnostic/storage failure must not destroy the dispatcher
                # either. Carry it back to the caller in the final stop reason.
                self.interruption += f'; cannot record browser stop: {error}'
            if loop is not None:
                loop.call_soon_threadsafe(cancel)
            return
        self.delivered = True
        raise KeyboardInterrupt(self.interruption)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_teardown(self, item, nextitem):
        self.tearing_down = True
        try:
            return (yield)
        finally:
            self.tearing_down = False

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node):
        node.workerinput["test_time_limit_deadline"] = self.deadline

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(self, item):
        # pytest 9.1's tmp_path teardown indexes this stash even when an
        # interrupted setup never produced its first report. Seed the same
        # empty result mapping that pytest's report hook normally creates.
        from _pytest.tmpdir import tmppath_result_key
        item.stash.setdefault(tmppath_result_key, {})

    def expired(self, signum, frame):
        if coverage_data_locked() or progress_emitting(self.config):
            self.start_retry()
            return
        self.interruption = f"--test-time-limit={self.seconds:g}s expired"
        self.interrupt()

    def start_retry(self):
        signal.setitimer(signal.ITIMER_REAL, .1)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_makereport(self, item, call):
        # SQLite can turn an exception from a Python progress callback into
        # OperationalError. Preserve a monitor stop as interruption rather
        # than reporting a spurious product failure at the next boundary.
        if self.interruption is not None:
            self.delivered = True
            raise KeyboardInterrupt(self.interruption)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self, session, exitstatus):
        self.stop()
        return (yield)

    def stop(self):
        if self.previous_handler is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self.previous_handler)
            self.previous_handler = None

    def cleanup(self):
        self.stop()
        if self.browser_patch is not None:
            self.browser_patch.undo()
            self.browser_patch = None


class RuntimeTarget(SessionLimit):
    def __init__(self, config, target, slack):
        super().__init__(config, 0)
        self.target = target
        self.slack = target / 10 if slack is None else slack
        self.worker = hasattr(config, "workerinput")
        self.selected = None
        self.completed = set()
        self.active = {}
        self.phases = {}
        self.directory = None

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self, session):
        self.started = _monotonic()
        if self.worker:
            self.state_path = Path(self.config.workerinput["test_runtime_state"])
        else:
            # Session-start hooks precede autouse fixtures. Allocate through
            # pytest directly so this stop flag also avoids global /tmp.
            self.directory = self.config._tmp_path_factory.mktemp('runtime-monitor')
            self.state_path = self.directory / "state.json"
            self.state_path.write_text(json.dumps({"stop": False}))
        # Workers read only their coordinator's stop flag. They never estimate
        # whole-suite pace from their own unequal file assignment.
        self.start_timer(1, 1)

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node):
        node.workerinput["test_runtime_state"] = str(self.state_path)

    def pytest_collection_finish(self, session):
        if not self.worker and not self.config.getoption("numprocesses", default=0):
            self.selected = len(session.items)

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        if self.selected is None:
            self.selected = len(ids)

    def pytest_runtest_logstart(self, nodeid, location):
        if not self.worker:
            self.active[nodeid] = {"nodeid": nodeid, "phase": "setup", "started": _monotonic()}

    def pytest_runtest_logreport(self, report):
        if not self.worker:
            self.phases.setdefault(report.nodeid, {})[report.when] = report.duration
            if report.nodeid in self.active:
                self.active[report.nodeid].update(
                    phase={"setup": "call", "call": "teardown", "teardown": "finished"}[report.when],
                    started=_monotonic())

    def pytest_runtest_logfinish(self, nodeid, location):
        if not self.worker:
            self.active.pop(nodeid, None)
            self.completed.add(nodeid)

    def expired(self, signum, frame):
        if coverage_data_locked() or progress_emitting(self.config):
            return
        if self.worker:
            try:
                state = json.loads(self.state_path.read_text())
                if (not isinstance(state, dict) or type(state.get('stop')) is not bool
                        or state['stop'] and not isinstance(state.get('message'), str)):
                    raise ValueError('Invalid runtime monitor state')
            except (OSError, ValueError) as error:
                self.interruption = f'Runtime monitor state unavailable: {error}'
                self.interrupt()
                return
            if state["stop"]:
                self.interruption = state["message"]
                self.interrupt()
            return
        now = _monotonic()
        elapsed = now - self.started
        earned = self.target * len(self.completed) / self.selected if self.selected else 0
        lag = elapsed - earned
        if lag <= self.slack:
            return
        signal.setitimer(signal.ITIMER_REAL, 0)
        inventory = f"{len(self.completed)}/{self.selected}" if self.selected is not None else "collection pending"
        message = (f"Runtime target {self.target:g}s: {inventory} completed in {elapsed:.1f}s; "
                   f"{lag:.1f}s behind the target line (allowance {self.slack:g}s)")
        self.interruption = message
        state = {"stop": True, "message": message}
        pending = self.state_path.with_suffix(".pending")
        try:
            pending.write_text(json.dumps(state))
            pending.replace(self.state_path)
        except OSError as error:
            # Missing state is a terminal stop for workers too. Invalidating
            # the existing flag requires no new temporary-file allocation.
            self.interruption = message = f'{message}; cannot publish runtime stop: {error}'
            try:
                self.state_path.unlink(missing_ok=True)
            finally:
                self.interrupt()
            return
        try:
            self.report_stop(message, now, elapsed, lag)
        except Exception as error:
            self.interruption += f'; cannot record pace stop: {error}'
        self.interrupt()

    def report_stop(self, message, now, elapsed, lag):
        active = sorted(({"nodeid": row["nodeid"], "phase": row["phase"],
                          "seconds": now - row["started"]} for row in self.active.values()),
                        key=lambda row: -row["seconds"])
        slowest = sorted(({"nodeid": nodeid, "phases": phases, "seconds": sum(phases.values())}
                          for nodeid, phases in self.phases.items()), key=lambda row: -row["seconds"])[:10]
        progress = self.config.pluginmanager.getplugin("live-test-progress")
        if progress:
            progress.emit("pace_stop", message=message, target_seconds=self.target,
                          slack_seconds=self.slack, runtime_seconds=elapsed, lag_seconds=lag,
                          completed_tests=len(self.completed), selected_tests=self.selected,
                          active=active, slowest=slowest)
        terminal = self.config.pluginmanager.getplugin("terminalreporter")
        if terminal:
            if not progress:
                terminal.write_line(message)
            for row in active:
                terminal.write_line(f"Active {row['phase']} {row['seconds']:.3f}s: {row['nodeid']}")
            for row in slowest:
                terminal.write_line(f"Observed {row['seconds']:.3f}s {row['phases']}: {row['nodeid']}")
            terminal.flush()

    def cleanup(self):
        super().cleanup()
        # The pytest factory retains ownership through session/report teardown.
