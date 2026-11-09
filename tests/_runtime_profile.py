"""Temporary focused profiler for the #1169 implementation investigation."""
import cProfile
import io
from pathlib import Path
import pstats

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    profile = cProfile.Profile()
    profile.enable()
    try:
        yield
    finally:
        profile.disable()
        report = io.StringIO()
        pstats.Stats(profile, stream=report).sort_stats('cumulative').print_stats(70)
        path = Path('build/test-reports/1169-implementation/library-sync-full-workflow-profile.txt')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report.getvalue())
