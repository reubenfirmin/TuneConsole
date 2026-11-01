"""Saved preparation recovery retains originals, heads and unfinished work."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_collection_control_preparation import finish
from tests.core.test_saved_output_preparation import seed
from tests.core.test_saved_output_publication import publish
from yt_playlist.core import entity_names, policy_recovery, recommendation_outputs as files
from yt_playlist.core import saved_preparation_files as recovery
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.migrations import registry, runner
from yt_playlist.core.preparation import saved_outputs as preparation, saved_output_publication as publication
from yt_playlist.core.store import Store


@pytest.fixture
def installed(tmp_path):
    path = tmp_path / 'installed.db'
    setup = Store(path)
    try:
        runner.migrate(setup.conn, registry=registry.MIGRATIONS[:79])
    finally:
        setup.conn.close()
    store = Store(path)
    try:
        store.init_schema(defer_metadata=True)
        yield store, 79
    finally:
        store.conn.close()


def _prepare(store, kind):
    expected, _ = seed(store)
    # Retain an explicitly unresolved policy, isolating file custody from the
    # separate policy collector's reference invalidation rules.
    with store.conn:
        store.conn.execute('DELETE FROM recommendation_policies')
    for name in preparation.SOURCES:
        publish(store.conn, name)
    if kind == 'unfinished':
        expected['radio_seed_offer'] = json.dumps({'menu_id': 'next', 'tracks': [
            {'video_id': video} for video in ('present-video', 'missing-video', 'present-video')]})
        store.settings.set_setting('radio_seed_offer', expected['radio_seed_offer'])
        progress = preparation.JOBS['radio-seed'].step(store.conn, batch_size=1)
        assert progress.status != 'prepared'
    elif kind == 'native-precedence':
        entity_names.clear_predecessor_reads(store.conn)
        Revisions.unbind(store.conn)
        runner.migrate(store.conn, registry=registry.MIGRATIONS[:95])
        entity_names.predecessor_reads(store.conn)
        store._snapshot_revisions = Revisions(store.conn)
        store._bind_repositories()
    return expected


def _originals(conn):
    return {(owner, identity): (source, digest, size, files.read(filename, digest, size,
                surface=source, chunk_key=key))
            for owner, identity, source, key, filename, digest, size in recovery.references(conn)}


def _custody(conn):
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' "
        "AND (name LIKE 'prepared_radio_seed_%' OR name LIKE 'prepared_autotune_report_%' "
        "OR name LIKE 'published_radio_seed_%' OR name LIKE 'published_autotune_report_%') ORDER BY name")]
    result = {}
    for table in tables:
        columns = [row[1] for row in conn.execute(f'PRAGMA table_info({table})') if row[1] != 'filename']
        result[table] = sorted(map(tuple, conn.execute('SELECT ' + ','.join(columns) + ' FROM ' + table)), key=repr)
    return result


def _hashes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.glob('*/*.json')}


@pytest.mark.parametrize('kind', ['selected', 'unfinished', 'native-precedence'])
@pytest.mark.parametrize('original', [False, True], ids=['compact', 'verified-backup'])
def test_recovered_saved_outputs_own_exact_history_and_continue_without_source(
        installed, tmp_path, monkeypatch, kind, original):
    producer, _ = installed
    expected = _prepare(producer, kind)
    before = list(producer.conn.iterdump())
    originals, custody = _originals(producer.conn), _custody(producer.conn)
    root = recovery.directory(producer.conn)
    hashes = _hashes(root)
    selected = {source: publication.selected(producer.conn, source) for source in expected}
    report = policy_recovery.recover(producer._db_path, tmp_path / 'recovered', now=10000, progress=lambda _: None)
    assert list(producer.conn.iterdump()) == before
    assert _hashes(root) == hashes
    assert 'saved_preparation' in report['verified_backup_file_manifests']
    assert not (tmp_path / 'recovered' / 'working.db.preparation').exists()
    with closing(sqlite3.connect(report['verified_backup'])) as backup:
        assert list(backup.iterdump()) == before
        if original:
            path = tmp_path / 'restored.db'
            with closing(sqlite3.connect(path)) as destination:
                backup.backup(destination)
        else:
            path = Path(report['compact_database'])
    candidate = Store(path)
    hidden = root.with_name(root.name + '-unavailable')
    root.rename(hidden)
    try:
        if original:
            policy_recovery._restore_files(candidate.conn, report['verified_backup_file_manifests'])
        candidate.init_schema(defer_metadata=True)
        assert _originals(candidate.conn) == originals
        assert _custody(candidate.conn) == custody
        assert all(Path(row[4]).is_relative_to(recovery.directory(candidate.conn))
                   for row in recovery.references(candidate.conn))
        publication.validate_installed(candidate.conn)
        assert {source: publication.selected(candidate.conn, source) for source in expected} == selected
        if kind == 'native-precedence':
            def wrong_owner(*args):
                pytest.fail('Native saved outputs fell back to the installed preparation')
            monkeypatch.setattr(preparation, 'read_original', wrong_owner)
        assert {source: candidate.settings.get_setting(source) for source in expected} == expected
        if kind == 'unfinished':
            job = preparation.JOBS['radio-seed']
            revision = finish(job, candidate.conn, size=1).prepared_revision
            publication.publish(candidate.conn, 'radio_seed_offer', revision=revision)
            assert candidate.settings.get_setting('radio_seed_offer') == expected['radio_seed_offer']
            assert len(_originals(candidate.conn)) == len(originals)
        assert not candidate.conn.execute('PRAGMA foreign_key_check').fetchone()
        owner = preparation.tables('radio_seed_offer')[0]
        with pytest.raises(sqlite3.IntegrityError, match='retained'):
            with candidate.conn:
                candidate.conn.execute(f'UPDATE {owner} SET raw_size=0')
    finally:
        candidate.conn.close()
        hidden.rename(root)
    assert list(producer.conn.iterdump()) == before
    assert _hashes(root) == hashes


def test_interrupted_saved_relocation_rolls_back_files_guards_and_custody(installed, tmp_path, monkeypatch):
    store, _ = installed
    _prepare(store, 'selected')
    with store.conn:
        store.conn.execute('BEGIN')
        manifest = recovery.export(store.conn, tmp_path / 'backup')
    before, hashes = list(store.conn.iterdump()), _hashes(recovery.directory(store.conn))
    calls = 0

    def interrupted():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise InterruptedError('second saved owner relocation')

    with monkeypatch.context() as patch:
        patch.setattr(recovery, 'check_cancellable_work', interrupted)
        with pytest.raises(InterruptedError, match='second saved owner'):
            recovery.restore(store.conn, manifest)
    assert not store.conn.in_transaction
    assert list(store.conn.iterdump()) == before
    assert _hashes(recovery.directory(store.conn)) == hashes
    publication.validate_installed(store.conn)
    recovery.restore(store.conn, manifest)
    assert _custody(store.conn)
    publication.validate_installed(store.conn)


def test_missing_saved_file_cannot_report_success(installed, tmp_path):
    store, _ = installed
    _prepare(store, 'selected')
    before = list(store.conn.iterdump())
    path = Path(next(recovery.references(store.conn))[4])
    hidden = path.with_suffix('.unavailable')
    path.rename(hidden)
    try:
        with pytest.raises(FileNotFoundError):
            policy_recovery.recover(store._db_path, tmp_path / 'failed', progress=lambda _: None)
        assert not (tmp_path / 'failed' / 'report.json').exists()
        assert list(store.conn.iterdump()) == before
    finally:
        hidden.rename(path)
