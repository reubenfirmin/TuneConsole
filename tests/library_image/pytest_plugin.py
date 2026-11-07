"""Explicit plugin for one selected test per local image workspace.

The image runner loads this with -p. Ordinary pytest and CI do not load it.
"""
import json
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption('--library-collection-report')
    parser.addoption('--library-node-id')


def pytest_collection_modifyitems(config, items):
    selected = config.getoption('--library-node-id')
    if not config.option.collectonly and not selected:
        raise pytest.UsageError('Run managed pytest through scripts/test-library.py for a fresh workspace per test')
    if selected:
        others = [item for item in items if item.nodeid != selected]
        items[:] = [item for item in items if item.nodeid == selected]
        config.hook.pytest_deselected(items=others)
        if len(items) != 1:
            raise pytest.UsageError('Image workspace must select exactly one collected test')
    destination = config.getoption('--library-collection-report')
    if destination:
        Path(destination).write_text(json.dumps([item.nodeid for item in items]) + '\n')


@pytest.fixture
def managed_library():
    from .fixtures import managed_database
    database = managed_database()
    if database is None:
        pytest.skip('Run this actual-library check through scripts/test-library.py')
    return database
