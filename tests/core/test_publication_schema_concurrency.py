"""Repeated definition checks must leave the writer available to ordinary work."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
import threading

import pytest

from tests.store_fixture_support import numbered_store as installed
from yt_playlist.core.migrations import regular
from yt_playlist.core.preparation import (choice_publication, conflict_publication, demand_publication, discovery_artist_publication, discovery_checkpoint_publication,
    genre_preference_publication, genre_publication, home_publication, library_sync_publication, metadata_destinations, metadata_publication, recipe_publication, subject_publication, taste_publication)


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Real concurrent SQL validation does not construct the HTTP application."""


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('family', ['artist', 'listening', 'home', 'checkpoints', 'document', 'genre', 'preferences', 'recipes', 'demand', 'taste', 'choice', 'conflicts', 'metadata', 'metadata_destinations'])
def test_existing_schema_validation_allows_serving_writes_and_regular_ddl(
        installed, family, monkeypatch):
    store, _ = installed
    conn = store.conn
    if family in ('genre','preferences','demand','taste','metadata','metadata_destinations'):
        validate_owner = {'genre':genre_publication,'preferences':genre_preference_publication,
            'demand':demand_publication,'taste':taste_publication,'metadata':metadata_publication,
            'metadata_destinations':metadata_destinations}[family]
        if family=='preferences':
            genre_preference_publication.preparation.CHOICES.install(conn)
            genre_preference_publication.preparation.DEFAULTS.install(conn)
        else:
            validate_owner.preparation.JOB.install(conn)
        install = validate_owner.install
    elif family == 'listening':
        subject_publication.preparation.install(conn)
        install, validate_owner = subject_publication.install, subject_publication
    else:
        owner = {'artist': discovery_artist_publication.OWNERS['artists_legacy'],
                 'choice': choice_publication.OWNER,
                 'conflicts': conflict_publication.OWNER,
                 'home': home_publication.OWNER,
                 'checkpoints': discovery_checkpoint_publication.OWNERS['fetch-history'],
                 'recipes': recipe_publication.OWNER,
                 'document': library_sync_publication.RESULT}[family]
        owner.job.install(conn)
        if family == 'home':
            owner.roots.ROOT_JOB.install(conn)
        install, validate_owner = owner.install, type(owner)
    install(conn)
    entered, release = threading.Event(), threading.Event()
    validate_name = 'validate_installed' if family=='metadata_destinations' else 'validate'
    validate, worker_thread = getattr(validate_owner,validate_name), None
    def slow(*args):
        result = validate(*args)
        if threading.get_ident() == worker_thread:
            entered.set()
            assert release.wait(15)
        return result
    monkeypatch.setattr(validate_owner, validate_name, slow)
    def inspect():
        nonlocal worker_thread
        worker_thread = threading.get_ident()
        with closing(sqlite3.connect(store._db_path, timeout=.1)) as reader, reader:
            reader.row_factory = sqlite3.Row
            install(reader)
    with ThreadPoolExecutor() as pool:
        task = pool.submit(inspect)
        try:
            assert entered.wait(15)
            store.settings.set_setting('publication-concurrency-evidence', 'ordinary writer')
            regular.migrate(conn)
        finally:
            release.set()
        task.result(timeout=15)
    assert store.settings.get_setting('publication-concurrency-evidence') == 'ordinary writer'
