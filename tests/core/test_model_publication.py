"""Encoder cutover fences files and both jobs, including continuing model workers."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import numpy as np
import pytest

from tests.core.test_collection_control_preparation import finish
from tests.core.test_model_artifact_preparation import current, seed
from tests.core.test_predecessor_writes import installed
from yt_playlist.core import model_state, recommendation_outputs as files
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import model_artifacts as preparation, model_publication as publication
from yt_playlist.core.store import Store


def prepare(store):
    revisions = []
    for job in (preparation.ARTIFACT_JOB, preparation.FINGERPRINT_JOB):
        job.install(store.conn)
        revisions.append(finish(job, store.conn).prepared_revision)
    return dict(zip(('artifact_revision', 'fingerprint_revision'), revisions))


def rejection_inputs(store):
    sources = seed(store)
    revisions = prepare(store)
    assert publication.selected(store.conn) is None
    return sources, revisions


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_existing_and_reopened_runtime_matches_historical_owner(installed, tmp_path):
    store, _ = installed
    sources = seed(store)
    expected = Store(tmp_path / 'historical-encoder-owner.db')
    other = Store(store._db_path)
    try:
        store.conn.backup(expected.conn)
        runner.migrate(expected.conn, registry=registry.MIGRATIONS[:94])
        other.init_schema(defer_metadata=True)
        revisions = prepare(store)
        publication.publish(store.conn, **revisions)
        publication.publish(store.conn, **revisions)
        assert publication.selected(other.conn) == (0, 0)
        served = {key: model_state.get(expected.conn, key, 'missing') for key in sources}
        assert {key: other.settings.get_setting(key, 'missing') for key in sources} == served
        assert dict(store.conn.execute('SELECT key,value FROM settings')).items() >= sources.items()
        regular.validate(other.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        expected.conn.close()
        other.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert publication.selected(reopened.conn) == (0, 0)
        assert {key: reopened.settings.get_setting(key, 'missing') for key in sources} == served
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('key,raw', [('rec_content_model', '{"new":"recording"}'),
                                  ('rec_artist_content_model', ''), ('rec_content_generation', 'new-generation')])
def test_source_and_compatible_writes_withdraw_the_entire_selection(installed, key, raw):
    store, _ = installed
    sources = seed(store)
    publication.publish(store.conn, **prepare(store))
    old_heads = [dict(row) for row in current(store.conn)]
    with closing(sqlite3.connect(store._db_path)) as old_writer, old_writer:
        old_writer.execute('UPDATE settings SET value=? WHERE key=?', (raw, key))
    assert publication.selected(store.conn) is None
    assert model_state.available(store.conn)
    assert store.settings.get_setting(key, 'missing') == raw
    stale = {'artifact_revision': 0, 'fingerprint_revision': 0}
    with pytest.raises(MigrationError, match='reconciled preparation'):
        publication.publish(store.conn, **stale)
    publication.publish(store.conn, **prepare(store))
    assert store.settings.get_setting(key, 'missing') == ('missing' if key in preparation.ARTIFACTS and not raw else raw)
    assert all(preparation.read_original(store.conn, head) == sources[head['source']] for head in old_heads)
    for table in (publication.LINKS, publication.TOKENS, preparation.HEAD, preparation.PARTS):
        with pytest.raises(sqlite3.IntegrityError, match='(future cleanup|retained)'):
            with store.conn:
                store.conn.execute(f'DELETE FROM {table}')
    store.settings.set_values({'rec_content_model': '{"from":"worker"}', 'rec_content_inputs': 'worker-input'})
    assert publication.selected(store.conn) is None
    assert store.settings.get_setting('rec_content_inputs') == 'worker-input'
    store.settings.delete_setting('rec_content_model')
    assert store.settings.get_setting('rec_content_model', 'missing') is None


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('mutation', ['encoder', 'fingerprint', 'manifest'])
def test_continuing_writes_during_file_verification_cannot_publish_stale_inputs(
        installed, staged_custody, monkeypatch, mutation):
    store, _ = installed
    _, revisions = staged_custody(installed, 'model-artifacts-and-fingerprints', rejection_inputs)
    real_read = files.read
    changed = False
    def change_during_read(*args, **kwargs):
        nonlocal changed
        assert not store.conn.in_transaction
        if not changed:
            changed = True
            if mutation == 'manifest':
                with store.conn:
                    store.conn.execute(f"UPDATE {preparation.PARTS} SET filename=filename||'.changed' WHERE position=0")
            else:
                key = 'rec_content_model' if mutation == 'encoder' else 'rec_fit_inputs'
                store.settings.set_setting(key, '{"new":"source"}' if mutation == 'encoder' else 'new-token')
        return real_read(*args, **kwargs)
    monkeypatch.setattr(files, 'read', change_during_read)
    with pytest.raises(MigrationError, match='(reconciled preparation|changed during publication)'):
        publication.publish(store.conn, **revisions)
    assert publication.selected(store.conn) is None
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['missing-file', 'fingerprint', 'original'])
def test_incomplete_or_corrupt_preparation_does_not_select_a_model(installed, staged_custody, failure):
    store, _ = installed
    sources, revisions = staged_custody(installed, 'model-artifacts-and-fingerprints', rejection_inputs)
    if failure == 'missing-file':
        Path(store.conn.execute(f'SELECT filename FROM {preparation.PARTS} LIMIT 1').fetchone()[0]).unlink()
    elif failure == 'fingerprint':
        with store.conn:
            store.conn.execute(f"UPDATE {preparation.FINGERPRINT_TABLE} SET token='incorrect' WHERE source='rec_content_generation'")
    else:
        # Model a corrupt file from outside this SQL connection. The manifest
        # still points to the original digest, so no selected generation exists.
        path = Path(store.conn.execute(f'SELECT filename FROM {preparation.PARTS} LIMIT 1').fetchone()[0])
        path.write_bytes(b'corrupt encoder file')
    with pytest.raises((MigrationError, ValueError, FileNotFoundError)):
        publication.publish(store.conn, **revisions)
    assert publication.selected(store.conn) is None
    assert all(store.settings.get_setting(key) == raw for key, raw in sources.items())
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_publication_interruption_rolls_back_then_explicit_retry_succeeds(installed):
    store, _ = installed
    seed(store)
    revisions = prepare(store)
    def interrupt():
        raise KeyboardInterrupt('controlled encoder cutover interruption')
    with pytest.raises(KeyboardInterrupt):
        publication.publish(store.conn, **revisions, clock=interrupt)
    assert not store.conn.in_transaction
    assert publication.selected(store.conn) is None
    regular.migrate(store.conn)
    publication.publish(store.conn, **revisions)
    assert publication.selected(store.conn) == (0, 0)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_failure_after_link_copy_retains_old_generation_without_partial_selection(installed):
    store, _ = installed
    seed(store)
    publication.publish(store.conn, **prepare(store))
    store.settings.set_setting('rec_content_inputs', 'next-input')
    revisions = prepare(store)
    store.conn.execute(f'CREATE TEMP TRIGGER interrupt_model_cutover BEFORE INSERT ON main.{publication.TOKENS} '
                       "BEGIN SELECT RAISE(ABORT,'controlled cutover failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled cutover'):
        publication.publish(store.conn, **revisions)
    assert publication.selected(store.conn) is None
    assert store.settings.get_setting('rec_content_inputs') == 'next-input'
    assert list(map(tuple, store.conn.execute(f'SELECT artifact_revision,fingerprint_revision FROM {publication.GENERATIONS}'))) == [(0, 0)]
    assert store.conn.execute(f'SELECT count(*) FROM {publication.LINKS}').fetchone()[0] == 2
    store.conn.execute('DROP TRIGGER temp.interrupt_model_cutover')
    publication.publish(store.conn, **revisions)
    assert publication.selected(store.conn) == (0, 1)


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('raw', [None, '', '{}'])
def test_null_blank_empty_and_missing_models_preserve_their_exact_sources(installed, raw):
    store, _ = installed
    with store.conn:
        store.conn.execute("INSERT INTO settings VALUES ('rec_content_model',?)", (raw,))
    publication.publish(store.conn, **prepare(store))
    assert store.settings.get_setting('rec_content_model', 'missing') == (raw or 'missing')
    assert store.settings.get_setting('rec_artist_content_model', 'missing') == 'missing'
    assert preparation.read_original(store.conn, current(store.conn)[0]) == raw
    with store.conn:
        store.conn.execute("DELETE FROM settings WHERE key='rec_content_model'")
    publication.publish(store.conn, **prepare(store))
    assert store.settings.get_setting('rec_content_model', 'missing') == 'missing'
    assert store.conn.execute(f'SELECT count(*) FROM {publication.LINKS}').fetchone()[0] == 1


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_real_content_generation_publication_keeps_models_tokens_and_vectors_together(installed):
    store, _ = installed
    seed(store)
    publication.publish(store.conn, **prepare(store))
    model = json.dumps({'ncat': 1, 'cat': {'fam:techno': 0}, 'cont': []})
    identity = store.tracks.upsert_track('worker-upload', 'Worker Song', 'Worker Artist', None, None)
    key = store.conn.execute('SELECT identity_key FROM tracks WHERE id=?', (identity,)).fetchone()[0]
    vector = np.array([1], dtype=np.float32).tobytes()
    assert store.rec_model.publish_content_generation([(key, vector)], [], model, 'genre-revision', 'worker-input', '0004')
    generation = store.rec_model.content_generation()
    assert generation['model'] == model
    assert generation['revision'] == 'genre-revision'
    assert generation['inputs'] == 'worker-input'
    assert generation['coherent'] and generation['library'] == [(key, vector)]
    assert publication.selected(store.conn) is None
    publication.publish(store.conn, **prepare(store))
    assert store.rec_model.content_generation() == generation
    assert not store.rec_model.publish_content_generation([], [], model, 'stale', 'stale', '0004')
    assert store.rec_model.content_generation() == generation


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_snapshot_guards_consumed_model_but_ignores_reconciliation_counters(installed):
    store, _ = installed
    sources = seed(store)
    publication.publish(store.conn, **prepare(store))
    with store.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting('rec_content_model') == sources['rec_content_model']
        store.settings.set_setting('unrelated-progress', 'working')
        assert snapshot.publish(lambda: None)
    store.settings.set_setting('rec_fit_inputs', 'new-input')
    with store.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting('rec_content_model') == sources['rec_content_model']
        prepare(store)
        assert snapshot.publish(lambda: None)
    with store.read_snapshot() as snapshot:
        snapshot.store.settings.get_setting('rec_content_model')
        store.settings.set_setting('rec_content_model', '{"new":"consumed"}')
        assert not snapshot.publish(lambda: pytest.fail('published stale encoder inputs'))


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('target', ['capture', 'published-part'])
def test_structural_admission_rejects_missing_guards(installed, target):
    store, _ = installed
    seed(store)
    publication.publish(store.conn, **prepare(store))
    store.conn.execute('BEGIN')
    try:
        trigger = ('prepare_content_encoders_settings_update' if target == 'capture'
                   else 'published_' + preparation.PARTS + '_delete')
        store.conn.execute('DROP TRIGGER ' + trigger)
        with pytest.raises(MigrationError, match='(preparation|publication) object'):
            regular.validate(store.conn)
    finally:
        store.conn.rollback()
