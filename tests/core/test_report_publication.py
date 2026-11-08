"""Prepared reports select verified files and atomically withdraw on source edits."""
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.core.test_report_preparation import VALUE, finish, head, seed
from tests.store_fixture_support import publication_consumer_reads, reuse_reporting_reader
from yt_playlist.core import recommendation_outputs as files, recommendation_reports as reports, schema_inspection
from yt_playlist.core.migrations import MigrationError, migrate, regular, registry, runner
from yt_playlist.core.preparation import recommendation_reports as preparation, report_publication as publication
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Report publication has no HTTP lifecycle or scheduled library sync."""


def prepare(conn):
    preparation.JOB.install(conn)
    return finish(conn).prepared_revision




@pytest.mark.parametrize('installed', [79, 92], indirect=True)
def test_publication_reads_files_and_typed_usage_while_continuing_writers_withdraw_it(installed, monkeypatch):
    store, _ = installed
    raw = seed(store)
    conn = store.conn
    version = conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
    revision = prepare(conn)
    assert not reports.prepared_owner(conn)
    publication.publish(conn, revision=revision)
    assert reports.prepared_owner(conn)
    regular.validate(conn)
    assert store.rec_proposals.get_proposals('listening_choices') == VALUE
    assert store.rec_proposals.has_proposals('listening_choices')
    assert not store.rec_proposals.has_proposals('model_health')
    assert store.rec_proposals.choice_usage() == VALUE['home_format']
    assert store.rec_proposals.report_checkpoint('listening_choices')['generation'] == 'exact-generation'
    assert conn.execute("SELECT payload FROM rec_proposals WHERE surface='listening_choices'").fetchone()[0] == raw
    previous = head(conn)
    # The prepared metadata path remains usable without reading diagnostic files.
    original_read = files.read
    monkeypatch.setattr(files, 'read', lambda *args, **kwargs: pytest.fail('Metadata read opened a report file'))
    assert store.rec_proposals.choice_usage() == VALUE['home_format']
    assert store.rec_proposals.report_checkpoint('listening_choices')['at'] == 98
    assert store.rec_proposals.has_proposals('listening_choices')
    monkeypatch.setattr(files, 'read', original_read)
    # Installed writers remain the continuing source. No stale prepared value
    # survives the same transaction that replaces it.
    replacement = {'at': 202, 'home_format': {'scores': {'radio': 9}}, 'explanation': 'Later output'}
    store.rec_proposals.put_proposals('listening_choices', replacement, 203)
    assert not reports.prepared_owner(conn)
    assert store.rec_proposals.get_proposals('listening_choices') == replacement
    assert store.rec_proposals.choice_usage() == replacement['home_format']
    next_revision = finish(conn).prepared_revision
    assert not reports.prepared_owner(conn)
    publication.publish(conn, revision=next_revision)
    assert reports.prepared_owner(conn)
    assert store.rec_proposals.get_proposals('listening_choices') == replacement
    assert preparation.read_original(conn, previous) == raw
    assert conn.execute(f'SELECT count(DISTINCT revision) FROM {publication.LINKS}').fetchone()[0] == 2
    reopened = Store(store._db_path)
    try:
        reopened.init_schema()
        with reuse_reporting_reader(reopened, monkeypatch):
            assert reopened.rec_proposals.get_proposals('listening_choices') == replacement
            assert reopened.rec_proposals.report_checkpoint('listening_choices')['at'] == 202
    finally:
        reopened.conn.close()
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version


@pytest.mark.parametrize('installed', [92], indirect=True)
def test_verification_releases_snapshot_and_rejects_concurrent_source_write(installed, monkeypatch):
    store, _ = installed
    seed(store)
    conn = store.conn
    revision = prepare(conn)
    second = Store(store._db_path)
    read = files.read
    calls = []
    try:
        def edit(*args, **kwargs):
            assert not conn.in_transaction
            if not calls:
                with second.conn:
                    second.conn.execute("UPDATE rec_proposals SET payload='{}' WHERE surface='listening_choices'")
                calls.append(True)
            return read(*args, **kwargs)
        monkeypatch.setattr(files, 'read', edit)
        with pytest.raises(MigrationError, match='changed during publication'):
            publication.publish(conn, revision=revision)
        assert not reports.prepared_owner(conn)
        assert store.rec_proposals.get_proposals('listening_choices') == {}
        assert not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (publication.HEAD,)).fetchone()
    finally:
        second.conn.close()


@pytest.mark.parametrize('installed', [92], indirect=True)
@pytest.mark.parametrize('mutation', ['metadata', 'usage', 'parts', 'source'])
def test_publication_rejects_unverified_fields_files_and_source_revisions(installed, mutation):
    store, _ = installed
    seed(store)
    conn = store.conn
    revision = prepare(conn)
    with conn:
        if mutation == 'metadata':
            conn.execute(f"UPDATE {preparation.HEAD} SET inputs_key='wrong' WHERE surface='listening_choices'")
        elif mutation == 'usage':
            conn.execute("UPDATE prepared_report_usage SET value=value+1")
        elif mutation == 'parts':
            conn.execute(f'DELETE FROM {preparation.PARTS} WHERE position=0')
        else:
            conn.execute("UPDATE rec_proposals SET built_at=102 WHERE surface='listening_choices'")
    with pytest.raises(MigrationError):
        publication.publish(conn, revision=revision)
    assert not reports.prepared_owner(conn)
    assert store.rec_proposals.get_proposals('listening_choices') == VALUE


@pytest.mark.parametrize('installed', [92], indirect=True)
def test_final_commit_interruption_and_idempotent_retry_preserve_exact_owners(installed):
    store, _ = installed
    raw = seed(store)
    conn = store.conn
    revision = prepare(conn)
    def interrupted():
        assert conn.execute(f'SELECT 1 FROM {publication.LINKS}').fetchone()
        raise KeyboardInterrupt('controlled report publication interruption')
    with pytest.raises(KeyboardInterrupt):
        publication.publish(conn, revision=revision, clock=interrupted)
    assert not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (publication.HEAD,)).fetchone()
    assert preparation.read_original(conn, head(conn)) == raw
    publication.publish(conn, revision=revision, clock=lambda: 10)
    before = list(conn.iterdump())
    publication.publish(conn, revision=revision, clock=lambda: 20)
    assert list(conn.iterdump()) == before
    with pytest.raises(sqlite3.IntegrityError, match='generation is retained'), conn:
        conn.execute(f"UPDATE {preparation.HEAD} SET inputs_key='replaced' WHERE surface='listening_choices'")
    with pytest.raises(sqlite3.IntegrityError, match='links are retained'), conn:
        conn.execute(f'DELETE FROM {publication.LINKS}')


@pytest.mark.parametrize('installed', [92], indirect=True)
def test_missing_diagnostic_file_does_not_remove_typed_serving_fields(installed):
    store, _ = installed
    seed(store)
    conn = store.conn
    publication.publish(conn, revision=prepare(conn))
    filename = conn.execute(f"SELECT filename FROM {preparation.PARTS} WHERE surface='listening_choices' LIMIT 1").fetchone()[0]
    Path(filename).unlink()
    assert store.rec_proposals.get_proposals('listening_choices') is None
    assert store.rec_proposals.has_proposals('listening_choices')
    assert store.rec_proposals.choice_usage() == VALUE['home_format']
    assert store.rec_proposals.report_checkpoint('listening_choices')['at'] == 98


@pytest.mark.parametrize('installed', [92], indirect=True)
def test_rename_delete_and_empty_publication_withdraw_all_stale_reports(installed):
    store, _ = installed
    seed(store)
    conn = store.conn
    publication.publish(conn, revision=prepare(conn))
    with conn:
        conn.execute("UPDATE rec_proposals SET surface='unrelated',payload='opaque' WHERE surface='listening_choices'")
        conn.execute("DELETE FROM rec_proposals WHERE surface='model_health'")
    assert not reports.prepared_owner(conn)
    publication.publish(conn, revision=finish(conn).prepared_revision)
    assert reports.prepared_owner(conn)
    assert store.rec_proposals.get_proposals('listening_choices') is None
    assert store.rec_proposals.choice_usage() == {}
    assert store.rec_proposals.report_checkpoint('model_health') is None
    assert conn.execute("SELECT payload FROM rec_proposals WHERE surface='unrelated'").fetchone()[0] == 'opaque'


@pytest.mark.parametrize('installed', [92], indirect=True)
def test_frozen_upgrade_route_keeps_genuine_v93_owner_authoritative(installed, tmp_path):
    store, _ = installed
    seed(store)
    conn = store.conn
    publication.publish(conn, revision=prepare(conn))
    converted = Store(tmp_path / 'explicit-frozen-upgrade.db')
    try:
        conn.backup(converted.conn)
        migrate(converted.conn, registry=registry.MIGRATIONS[:93])
        converted._bind_repositories()
        assert not reports.prepared_owner(converted.conn)
        regular.validate(converted.conn)
        assert converted.rec_proposals.get_proposals('listening_choices') == VALUE
        converted.rec_proposals.put_proposals('listening_choices', {'at': 300}, 301)
        assert converted.rec_proposals.report_checkpoint('listening_choices')['at'] == 300
        assert not converted.conn.execute("SELECT 1 FROM rec_proposals WHERE surface='listening_choices'").fetchone()
    finally:
        converted.conn.close()
