"""Completed SQL inputs below maintenance installation and HTTP checks."""
from contextlib import ExitStack, closing
from pathlib import Path
import sqlite3

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.prepared_fixture_support import prepared_template
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core.db_rebuild import build
from yt_playlist.core.migrations import registry, regular, runner


@pytest.fixture(scope='session')
def compaction_inputs(tmp_path_factory):
    """Completed schema admission below the file replacement boundary."""
    root = tmp_path_factory.mktemp('compaction-inputs')
    schemas, templates = {}, {}
    with ExitStack() as resources:
        def schema(version):
            if version not in schemas:
                with pytest.MonkeyPatch.context() as patch:
                    bound_catalog(patch)
                    result = resources.enter_context(closing(sqlite3.connect(':memory:')))
                    install_numbered_input(result, version, regular_owners=regular.CHANGES)
                    result.execute('PRAGMA query_only=ON')
                    schemas[version] = result
            return schemas[version]

        def get(version):
            if version not in templates:
                directory = root / str(version)
                directory.mkdir()
                with pytest.MonkeyPatch.context() as patch:
                    bound_catalog(patch)
                    templates[version] = resources.enter_context(prepared_template(
                        schema(version), lambda store: None, database=directory / 'library.db', defer_metadata=True))
            return templates[version]

        get.schema = schema
        yield get


@pytest.fixture
def completed_installed(request, tmp_path, compaction_inputs, monkeypatch):
    bound_catalog(monkeypatch)
    with compaction_inputs(request.param).open(tmp_path / 'library.db') as store:
        yield store, request.param


@pytest.fixture
def unfiltered_copy(monkeypatch):
    """Supply a real candidate below installation, not retention planning.

    Retained-copy tests own row filtering. Callers still exercise actual write
    admission, handle closure, file replacement, format validation and reopen.
    """
    def copy(source, output, *, progress, cancelled):
        assert not cancelled()
        with closing(sqlite3.connect(Path(source).as_uri() + '?mode=ro', uri=True)) as reader:
            version = runner.history(reader, registry.MIGRATIONS)
            progress('copy', {'table': 'settings'})
            with closing(sqlite3.connect(output)) as candidate:
                reader.backup(candidate)
        progress('verify', {'table': 'settings'})
        return {'installed_version': version}

    monkeypatch.setattr(build, 'build', copy)
