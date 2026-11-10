"""Supervise an isolated browser journey and retain evidence before stopping it.

Never interrupt Playwright's synchronous transport with a Python exception:
its teardown can then block on the interrupted command. The parent owns the
deadline and the worker's entire process group, including its browser driver.
"""
from collections import deque
from contextlib import contextmanager
import faulthandler
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
from uuid import uuid4

from tests.process_coverage import ChildCoverage, child_environment


class JourneyTimeout(AssertionError):
    pass


WORKER = 'TUNECONSOLE_BROWSER_JOURNEY_WORKER'


def supervise_journey(request, browser_name, report, *, seconds=120):
    """Return True in the worker; otherwise run that exact node in isolation."""
    node = request.node.nodeid
    if os.environ.get(WORKER) == node:
        return True
    report = Path(report).resolve()
    report.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, '-m', 'pytest', node, '-m', 'browser',
               '-p', 'pytest_coverage_guard', '-p', 'pytest_cov', '-p', 'tests.journey_profile',
               '--browser=' + browser_name, '--tb=short',
               '-o', 'faulthandler_timeout=90',
               '--junitxml=' + str(report / 'worker.xml'),
               '--output=' + str(report / 'playwright'), '--tracing=retain-on-failure']
    environment = child_environment()
    environment[WORKER] = node
    # All diagnostics live outside pytest's disposable fixture directories.
    environment['TUNECONSOLE_BROWSER_JOURNEY_REPORT'] = str(report)
    with TemporaryDirectory(prefix='tuneconsole-journey-workspace-') as directory, (report / 'worker.log').open('w') as output:
        workspace = Path(directory)
        coverage = ChildCoverage.optional(request, workspace, report)
        command.extend(coverage.pytest_arguments() if coverage else ['--no-cov'])
        if coverage:
            environment['TUNECONSOLE_CHILD_COVERAGE_MARKER'] = str(coverage.marker)
        (workspace / 'tmp').mkdir()
        environment['TMPDIR'] = str(workspace / 'tmp')
        environment['TUNECONSOLE_JOURNEY_PROFILE'] = str(report / ('timing-' + uuid4().hex + '.jsonl'))
        environment['TUNECONSOLE_JOURNEY_STARTED'] = str(time.monotonic())
        started = time.monotonic()
        command.append('--basetemp=' + str(workspace / 'fixtures'))
        worker = subprocess.Popen(command, cwd=request.config.rootpath,
                                  env=environment, stdin=subprocess.DEVNULL,
                                  stdout=output, stderr=subprocess.STDOUT,
                                  start_new_session=True)
        try:
            try:
                result = worker.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                raise JourneyTimeout(
                    f'{node} exceeded {seconds}s; operation, request and thread diagnostics: {report}') from None
        finally:
            cleanup_started = time.monotonic()
            # Restrict signals to the session this supervisor created. Also
            # stop descendants left behind after an abnormal worker exit.
            try:
                os.killpg(worker.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(worker.pid, signal.SIGKILL)
                worker.wait(timeout=5)
            # A dead coordinator does not establish that its browser and
            # application descendants honored TERM. Finish the owned group
            # before removing its private fixture/file-domain workspace.
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            (report / 'worker-timing.json').write_text(json.dumps({
                'nodeid': node, 'worker_seconds': time.monotonic() - started,
                'wait_seconds': cleanup_started - started,
                'group_cleanup_seconds': time.monotonic() - cleanup_started,
                'exit_code': worker.returncode,
                'profile': environment['TUNECONSOLE_JOURNEY_PROFILE']}, indent=2) + '\n')
            if coverage:
                coverage.finish(worker, completed=worker.returncode == 0)
    assert result == 0, (report / 'worker.log').read_text()
    return False


@contextmanager
def browser_diagnostics(page, report, *, seconds=90):
    report = Path(report)
    report.mkdir(parents=True, exist_ok=True)
    events = deque(maxlen=100)
    started = time.monotonic()
    operation = 'starting browser journey'
    dumping = threading.Lock()
    def record(kind, detail):
        events.append({'seconds': round(time.monotonic() - started, 3),
                       'kind': kind, 'detail': detail})
    def checkpoint(name):
        nonlocal operation
        operation = name
        record('operation', name)
        from tests.journey_profile import record as profile
        profile('operation', operation=name)
        dump()
    def dump():
        with dumping:
            (report / 'operations.json').write_text(json.dumps(
                {'operation': operation, 'elapsed': time.monotonic() - started,
                 'events': list(events)}, indent=2) + '\n')
            with (report / 'threads.log').open('a') as output:
                faulthandler.dump_traceback(file=output, all_threads=True)
    def requested(request):
        record('request', request.method + ' ' + request.url)
    def finished(request):
        record('finished', request.url)
    def failed(request):
        record('failed', request.url + ': ' + str(request.failure))
    def console(message):
        if message.text.startswith('journey-stack:'):
            record('browser-stack', message.text.removeprefix('journey-stack:'))
    listeners = {'request': requested, 'requestfinished': finished,
                 'requestfailed': failed, 'console': console}
    watchdog = threading.Timer(seconds, dump)
    watchdog.daemon = True
    try:
        for event, listener in listeners.items():
            page.on(event, listener)
        # Record the JavaScript initiating stack before an HTTP request can
        # stall. Console events arrive through the existing browser transport;
        # the watchdog never makes another potentially blocked browser call.
        script = """(() => {
          if (window.__journeyDiagnostics) return;
          window.__journeyDiagnostics = true;
          const report = event => console.debug('journey-stack:' + JSON.stringify({
            event, url: location.href, stack: new Error().stack}));
          const fetch = window.fetch;
          window.fetch = function(...args) { report('fetch ' + args[0]); return fetch.apply(this, args); };
          const send = XMLHttpRequest.prototype.send;
          XMLHttpRequest.prototype.send = function(...args) { report('xhr.send'); return send.apply(this, args); };
          addEventListener('error', event => report('error ' + event.error?.stack));
        })();"""
        page.add_init_script(script)
        page.evaluate(script)
        watchdog.start()
        yield checkpoint
    except BaseException:
        dump()
        raise
    finally:
        watchdog.cancel()
        if watchdog.ident is not None:
            watchdog.join()
        for event, listener in listeners.items():
            page.remove_listener(event, listener)
