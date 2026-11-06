"""Child-only timing receipts; fixture durations are inclusive, not additive."""
import json
import os
from pathlib import Path
import time

import pytest


def record(event, **values):
    path = os.environ.get('TUNECONSOLE_JOURNEY_PROFILE')
    if path:
        started = float(os.environ['TUNECONSOLE_JOURNEY_STARTED'])
        with Path(path).open('a') as stream:
            stream.write(json.dumps({'event': event, 'seconds': time.monotonic() - started, **values}) + '\n')


record('plugin_loaded')


@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session):
    record('session_start')
    marker = os.environ.get('TUNECONSOLE_CHILD_COVERAGE_MARKER')
    if marker:
        from tests.process_coverage import sigterm_snapshot

        controller = session.config.pluginmanager.getplugin('_cov').cov_controller
        restore = sigterm_snapshot(lambda: controller.cov, marker,
                                  os.environ['TUNECONSOLE_BROWSER_JOURNEY_WORKER'])
        session.config.add_cleanup(restore)


def pytest_collection_finish(session):
    record('collection_finish', nodes=[item.nodeid for item in session.items])


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    started = time.monotonic()
    try:
        return (yield)
    finally:
        record('fixture_setup', fixture=fixturedef.argname, scope=fixturedef.scope,
               duration=time.monotonic() - started)


def pytest_runtest_logreport(report):
    record('phase', nodeid=report.nodeid, phase=report.when,
           duration=report.duration, outcome=report.outcome)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    record('session_finish', exitstatus=int(exitstatus))
    marker = os.environ.get('TUNECONSOLE_CHILD_COVERAGE_MARKER')
    if marker:
        from tests.process_coverage import finalize_coverage

        controller = session.config.pluginmanager.getplugin('_cov').cov_controller
        finalize_coverage(controller.cov, marker,
                          os.environ['TUNECONSOLE_BROWSER_JOURNEY_WORKER'], reason='pytest')


def pytest_unconfigure(config):
    record('unconfigure')
