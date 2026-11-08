"""Album-note files reconcile live proposal edits without a serving cutover."""
import json
from pathlib import Path

import pytest

from tests.core.test_predecessor_writes import installed
from tests.store_fixture_support import numbered_store
from yt_playlist.core import albums, recommendation_outputs
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import album_notes as preparation
from yt_playlist.core.store import Store


NOTES = {'release': {'description': 'Paid result: exact prose', 'links': [{'url': 'https://example.org'}]},
         'unadmitted': {'description': None, 'positions': [2, 2, 1]},
         'third': {'label': 'Last good'}}


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Album-note preparation does not construct an HTTP application."""


def seed(store, value=NOTES):
    raw = json.dumps(value, ensure_ascii=False, indent=2)
    with store.conn:
        albums.observe(store.conn, 'release', title='Album', artist='Artist')
        store.conn.execute('INSERT INTO rec_proposals VALUES (?,?,?)', (preparation.SURFACE, raw, 123.5))
    return raw


def finish(conn):
    for _ in range(20):
        result = preparation.JOB.step(conn, batch_size=1)
        if result.status == 'prepared':
            return result
    pytest.fail('Album-note preparation did not finish')


def header(conn):
    generation = preparation.JOB.progress(conn).prepared_revision
    return conn.execute(f'SELECT * FROM {preparation.HEAD} WHERE generation=?', (generation,)).fetchone()


def prepared(conn, generation):
    return {row['browse_id']: recommendation_outputs.read(row['filename'], row['digest'], row['byte_size'],
                surface=preparation.SURFACE, chunk_key=row['browse_id'])
            for row in conn.execute(f'SELECT * FROM {preparation.FILES} WHERE generation=? ORDER BY position',
                                    (generation,))}


@pytest.mark.parametrize('numbered_store', [79, 83], indirect=True)
def test_bounded_files_retain_exact_originals_and_reconcile_replacements(numbered_store):
    store, version = numbered_store
    raw = seed(store)
    history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    preparation.JOB.install(store.conn)
    first = preparation.JOB.step(store.conn, batch_size=1)
    assert first.processed == 1 and first.status == 'copying'
    assert store.conn.execute(f'SELECT count(*) FROM {preparation.FILES}').fetchone()[0] == 1
    regular.migrate(store.conn)
    finish(store.conn)
    original = header(store.conn)
    assert original['notes_count'] == 3 and original['built_at'] == 123.5
    assert preparation.read_original(original) == raw
    assert prepared(store.conn, original['generation']) == NOTES
    assert tuple(store.conn.execute(f'SELECT album_provider_id,unresolved_reason FROM {preparation.FILES} '
        "WHERE generation=? AND browse_id='unadmitted'", (original['generation'],)).fetchone()) == (
            None, 'provider_album_not_admitted')
    assert store.rec_proposals.get_proposals(preparation.SURFACE) == NOTES
    assert not store.conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='album_note_outputs'").fetchone()
    files = [row[0] for row in store.conn.execute(f'SELECT filename FROM {preparation.FILES}')]
    updated = {'release': {'description': 'Newer result'}, 'fourth': {}}
    store.rec_proposals.put_proposals(preparation.SURFACE, updated, 200)
    assert preparation.JOB.progress(store.conn).prepared_revision is None
    finish(store.conn)
    assert prepared(store.conn, header(store.conn)['generation']) == updated
    assert prepared(store.conn, original['generation']) == NOTES
    assert preparation.read_original(original) == raw
    # Admission changes also invalidate a generation without modifying the
    # source proposal, and must retain the formerly unresolved observation.
    with store.conn:
        albums.observe(store.conn, 'fourth', title='Fourth', artist='Artist')
    assert preparation.JOB.progress(store.conn).prepared_revision is None
    finish(store.conn)
    assert store.conn.execute(f'SELECT album_provider_id FROM {preparation.FILES} WHERE generation=? '
                             "AND browse_id='fourth'", (header(store.conn)['generation'],)).fetchone()[0] == 'fourth'
    with store.conn:
        store.conn.execute("UPDATE rec_proposals SET surface='retained-other' WHERE surface=?", (preparation.SURFACE,))
    finish(store.conn)
    assert header(store.conn)['source_present'] == 0 and header(store.conn)['notes_count'] == 0
    assert preparation.read_original(header(store.conn)) is None
    assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
    assert version == store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
    assert recommendation_outputs.collect_orphans(store.conn) == 0
    assert all(Path(filename).exists() for filename in files)


@pytest.mark.parametrize('installed', [83], indirect=True)
def test_file_publication_runs_without_transaction_and_rejects_stale_source(installed, monkeypatch):
    store, _ = installed
    seed(store)
    preparation.JOB.install(store.conn)
    publish = recommendation_outputs.publish
    second = Store(store._db_path)
    calls = []
    try:
        def concurrent_edit(root, encoded):
            assert not store.conn.in_transaction
            if not calls:
                with second.conn:
                    second.conn.execute('UPDATE rec_proposals SET payload=? WHERE surface=?',
                                        (json.dumps({'latest': {'label': 'Keep latest'}}), preparation.SURFACE))
                calls.append(True)
            return publish(root, encoded)
        monkeypatch.setattr(recommendation_outputs, 'publish', concurrent_edit)
        result = preparation.JOB.step(store.conn, batch_size=1)
        assert result.input_revision == 1 and result.prepared_revision is None
        assert not store.conn.execute(f'SELECT 1 FROM {preparation.HEAD}').fetchone()
        assert not store.conn.execute(f'SELECT 1 FROM {preparation.FILES}').fetchone()
        finish(store.conn)
        assert prepared(store.conn, header(store.conn)['generation']) == {'latest': {'label': 'Keep latest'}}
    finally:
        second.conn.close()


@pytest.mark.parametrize('installed', [83], indirect=True)
def test_restart_and_explicit_file_failure_recovery_preserve_prior_batches(
        installed, monkeypatch):
    store, _ = installed
    raw = seed(store)
    preparation.JOB.install(store.conn)
    checkpoint = preparation.JOB.step(store.conn, batch_size=1)
    publish = recommendation_outputs.publish
    def interrupted(*args):
        assert not store.conn.in_transaction
        raise KeyboardInterrupt('controlled interruption')
    monkeypatch.setattr(recommendation_outputs, 'publish', interrupted)
    with pytest.raises(KeyboardInterrupt):
        preparation.JOB.step(store.conn, batch_size=1)
    assert preparation.JOB.progress(store.conn) == checkpoint
    def disk_failure(*args):
        assert not store.conn.in_transaction
        raise OSError('controlled disk failure')
    monkeypatch.setattr(recommendation_outputs, 'publish', disk_failure)
    with pytest.raises(OSError, match='controlled disk failure'):
        preparation.JOB.step(store.conn, batch_size=1)
    assert preparation.JOB.progress(store.conn).status == 'failed'
    regular.migrate(store.conn)
    assert store.rec_proposals.get_proposals(preparation.SURFACE) == NOTES
    monkeypatch.setattr(recommendation_outputs, 'publish', publish)
    reopened = Store(store._db_path)
    try:
        reopened.init_schema()
        with pytest.raises(MigrationError, match='retry explicitly'):
            preparation.JOB.step(reopened.conn)
        preparation.JOB.step(reopened.conn, batch_size=1, retry=True)
        finish(reopened.conn)
        assert preparation.read_original(header(reopened.conn)) == raw
        assert prepared(reopened.conn, header(reopened.conn)['generation']) == NOTES
        assert reopened.conn.execute(f'SELECT count(*) FROM {preparation.FILES}').fetchone()[0] == 3
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('payload', ['null', '[]', '{"release":"unclassified"}', '{"release":{"x":1e999}}',
                                   json.dumps({str(i): {} for i in range(129)}),
                                   json.dumps({'release': {'text': 'x' * recommendation_outputs.MAX_BYTES}})])
@pytest.mark.parametrize('installed', [83], indirect=True)
def test_invalid_or_oversized_notes_fail_durably_without_replacing_source(installed, payload):
    store, _ = installed
    with store.conn:
        store.conn.execute('INSERT INTO rec_proposals VALUES (?,?,?)', (preparation.SURFACE, payload, 100))
    preparation.JOB.install(store.conn)
    with pytest.raises((MigrationError, ValueError)):
        preparation.JOB.step(store.conn)
    assert preparation.JOB.progress(store.conn).status == 'failed'
    assert store.conn.execute('SELECT payload FROM rec_proposals WHERE surface=?', (preparation.SURFACE,)).fetchone()[0] == payload
    assert not store.conn.execute(f'SELECT 1 FROM {preparation.HEAD}').fetchone()


@pytest.mark.parametrize('installed', [83], indirect=True)
@pytest.mark.parametrize('value', [None, {}])
def test_missing_and_empty_outputs_remain_distinct(installed, value):
    store, _ = installed
    raw = seed(store, value) if value is not None else None
    preparation.JOB.install(store.conn)
    finish(store.conn)
    assert header(store.conn)['notes_count'] == 0
    assert header(store.conn)['source_present'] == (value is not None)
    assert preparation.read_original(header(store.conn)) == raw
