"""Select owned temporary storage before coverage imports SQLite.

SQLite's Unix VFS caches its environment when the library initializes. A
session fixture is too late, particularly with pytest-cov. Keep this lifetime
outside report generation as well as fixture teardown.
"""
import os
from pathlib import Path
import shutil
import tempfile
import uuid

import pytest
from _pytest.tmpdir import TempPathFactory


def addoption(parser):
    if getattr(parser, '_fixture_storage_options_added', False):
        return
    parser._fixture_storage_options_added = True
    parser.addoption('--fixture-root', default=None, metavar='PATH',
        help='Parent for unique disposable fixture directories (default: checkout build/pytest). '
             'An explicit --basetemp takes precedence.')


def prepare(config):
    patch = pytest.MonkeyPatch()
    config.add_cleanup(patch.undo)
    options = config.known_args_namespace
    root = (Path(options.basetemp).absolute().parent if options.basetemp else
            Path(options.fixture_root or Path(__file__).parent / 'build' / 'pytest'))
    root.mkdir(parents=True, exist_ok=True)
    # Do not initialize the requested basetemp here: pytest may collect no
    # matching tests, in which case its existing contents must be untouched.
    # Every process (including workers) owns a separate startup/SQLite factory
    # on the selected volume, until its coverage and reports have finished.
    factory = TempPathFactory(
        given_basetemp=root / f'run-sqlite-{os.getpid()}-{uuid.uuid4().hex}',
        trace=config.trace.get('tmpdir'), retention_count=0,
        retention_policy='all', _ispytest=True)
    directory = factory.mktemp('stdlib')

    def remove_owned_storage():
        factory._exit_stack.close()
        try:
            shutil.rmtree(factory.getbasetemp())
        except FileNotFoundError:
            pass  # An explicit pytest retention policy can clean first.

    config.add_cleanup(remove_owned_storage)
    config._sqlite_test_storage = directory
    patch.setenv('TMPDIR', str(directory))
    patch.setenv('SQLITE_TMPDIR', str(directory))
    patch.setattr(tempfile, 'tempdir', str(directory))
