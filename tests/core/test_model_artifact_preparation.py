"""Saved encoders and their admission tokens prepare without fitting or cutover."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from tests.core.test_collection_control_preparation import finish
from tests.core.test_predecessor_writes import installed
from yt_playlist.core import model_state, recommendation_outputs as files
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import model_artifacts as preparation
from yt_playlist.core.store import Store


def seed(store):
    sources = {'rec_content_model': json.dumps({'weights': [1, 2, .3], 'vocabulary': 'alpha' * 28000}, indent=2),
        'rec_artist_content_model': json.dumps({'artists': ['Álbum', '東京'], 'weights': []}, ensure_ascii=False),
        'rec_content_generation': '0004', 'rec_content_inputs': ' exact source fingerprint ',
        'rec_content_genre_revision': '', 'rec_fit_inputs': None, 'rec_artist_fit_inputs': 'artist-token'}
    with store.conn:
        store.conn.executemany('INSERT INTO settings VALUES (?,?)', sources.items())
    return sources


def current(conn):
    return conn.execute(f'SELECT g.* FROM {preparation.CURRENT} h JOIN {preparation.HEAD} g '
                        'USING(source,generation) ORDER BY source').fetchall()


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_individual_parts_exact_originals_and_tokens_without_runtime_cutover(installed):
    store, _ = installed
    sources = seed(store)
    artifacts, tokens = preparation.ARTIFACT_JOB, preparation.FINGERPRINT_JOB
    for job in (artifacts, tokens):
        job.install(store.conn)
    # The artist source has one part; the recording source has three. At most
    # one part is committed per invocation even when its document is longer.
    for _ in range(3):
        before = store.conn.execute(f'SELECT count(*) FROM {preparation.PARTS}').fetchone()[0]
        artifacts.step(store.conn, batch_size=1)
        after = store.conn.execute(f'SELECT count(*) FROM {preparation.PARTS}').fetchone()[0]
        assert after - before <= 1
    assert finish(artifacts, store.conn).status == 'prepared'
    assert finish(tokens, store.conn).status == 'prepared'
    assert {row['source']: preparation.read_original(store.conn, row) for row in current(store.conn)} == {
        key: sources[key] for key in preparation.ARTIFACTS}
    assert dict(store.conn.execute(f'SELECT source,raw_value FROM {preparation.ORIGINALS}')) == {
        key: sources[key] for key in preparation.FINGERPRINTS}
    assert [tuple(row) for row in store.conn.execute(f'SELECT owner,field,source_kind,token FROM {preparation.FINGERPRINT_TABLE} '
                                                   "WHERE source IN ('rec_content_generation','rec_fit_inputs') ORDER BY source")] == [
        ('recording', 'generation', 'recorded', '0004'), ('recording', 'fit_fingerprint', 'null', None)]
    assert not model_state.available(store.conn)
    assert all(store.settings.get_setting(key) == value for key, value in sources.items())
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_frozen_upgrade_still_consumes_the_exact_retained_encoder_sources(installed, tmp_path):
    store, _ = installed
    sources = seed(store)
    for job in (preparation.ARTIFACT_JOB, preparation.FINGERPRINT_JOB):
        job.install(store.conn)
        finish(job, store.conn)
    upgraded = Store(tmp_path / 'encoder-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:94])
        for key, raw in sources.items():
            assert model_state.get(upgraded.conn, key) == raw
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_continuing_write_during_file_work_rejects_stale_checkpoint(installed, monkeypatch):
    store, _ = installed
    sources = seed(store)
    job = preparation.ARTIFACT_JOB
    job.install(store.conn)
    real_publish = files.publish
    changed = False
    def write_during_file(root, encoded):
        nonlocal changed
        assert not store.conn.in_transaction
        if not changed:
            changed = True
            store.settings.set_setting('rec_artist_content_model', '{"new":"artist encoder"}')
        return real_publish(root, encoded)
    monkeypatch.setattr(files, 'publish', write_during_file)
    assert job.step(store.conn, batch_size=1).input_revision == 1
    assert not current(store.conn)
    finish(job, store.conn)
    assert {row['source']: preparation.read_original(store.conn, row) for row in current(store.conn)} == {
        'rec_artist_content_model': '{"new":"artist encoder"}', 'rec_content_model': sources['rec_content_model']}
    old = [dict(row) for row in current(store.conn)]
    with store.conn:
        store.conn.execute("UPDATE settings SET key='retained-content-encoder' WHERE key='rec_content_model'")
    finish(job, store.conn)
    assert [row['source'] for row in current(store.conn)] == ['rec_artist_content_model']
    assert all(preparation.read_original(store.conn, row) for row in old)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_interrupt_disk_failure_independent_fingerprints_and_explicit_retry(installed, monkeypatch):
    store, _ = installed
    seed(store)
    artifacts, tokens = preparation.ARTIFACT_JOB, preparation.FINGERPRINT_JOB
    for job in (artifacts, tokens):
        job.install(store.conn)
    checkpoint = artifacts.step(store.conn, batch_size=1)
    def interrupt(*args):
        preparation.copy(*args)
        raise KeyboardInterrupt('controlled encoder interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(artifacts, copy=interrupt).step(store.conn, batch_size=1)
    assert artifacts.progress(store.conn) == checkpoint
    real_publish = files.publish
    def disk_failure(*args):
        assert not store.conn.in_transaction
        raise OSError('controlled disk failure')
    monkeypatch.setattr(files, 'publish', disk_failure)
    with pytest.raises(OSError, match='disk failure'):
        artifacts.step(store.conn, batch_size=1)
    assert artifacts.progress(store.conn).status == 'failed'
    assert finish(tokens, store.conn).status == 'prepared'
    regular.migrate(store.conn)
    monkeypatch.setattr(files, 'publish', real_publish)
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            artifacts.step(reopened.conn)
        artifacts.step(reopened.conn, batch_size=1, retry=True)
        assert finish(artifacts, reopened.conn).status == 'prepared'
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('raw,kind', [(None, 'null'), ('', 'blank'), ('{}', 'encoder')])
def test_null_blank_empty_encoder_and_deletion_remain_distinct(installed, raw, kind):
    store, _ = installed
    with store.conn:
        store.conn.execute("INSERT INTO settings VALUES ('rec_content_model',?)", (raw,))
    job = preparation.ARTIFACT_JOB
    job.install(store.conn)
    finish(job, store.conn)
    row, = current(store.conn)
    assert row['source_kind'] == kind
    assert preparation.read_original(store.conn, row) == raw
    store.settings.delete_setting('rec_content_model')
    finish(job, store.conn)
    assert not current(store.conn)
    assert preparation.read_original(store.conn, row) == raw


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_missing_part_is_detected_without_touching_the_source(installed):
    store, _ = installed
    sources = seed(store)
    job = preparation.ARTIFACT_JOB
    job.install(store.conn)
    finish(job, store.conn)
    path = store.conn.execute(f'SELECT filename FROM {preparation.PARTS} LIMIT 1').fetchone()[0]
    Path(path).unlink()
    with pytest.raises(FileNotFoundError):
        for row in current(store.conn):
            preparation.read_original(store.conn, row)
    assert all(store.settings.get_setting(key) == raw for key, raw in sources.items())


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('key,raw', [
    ('rec_content_model', 'null'), ('rec_content_model', '[]'),
    ('rec_content_model', '{"weights":[NaN]}'),
    ('rec_content_model', ' ' * (preparation.MAX_BYTES + 1)),
    ('rec_content_generation', 'x' * (64 * 1024 + 1)),
])
def test_invalid_or_oversized_source_fails_durably_and_keeps_installed_state(installed, key, raw):
    store, _ = installed
    store.settings.set_setting(key, raw)
    job = preparation.ARTIFACT_JOB if key in preparation.ARTIFACTS else preparation.FINGERPRINT_JOB
    job.install(store.conn)
    with pytest.raises((MigrationError, ValueError)):
        finish(job, store.conn)
    assert job.progress(store.conn).status == 'failed'
    assert store.settings.get_setting(key) == raw
    regular.migrate(store.conn)
