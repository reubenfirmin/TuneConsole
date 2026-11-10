"""Saved preparation recovery retains originals, heads and unfinished work."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from tests.core.test_collection_control_preparation import finish
from tests.core.test_saved_output_preparation import seed
from tests.core.test_saved_output_publication import publish
from tests import file_custody_support as fixture_files
from tests.prepared_fixture_support import assert_upgrade_phase, capture_template, custody_predecessor, custody_upgrade_cases
from yt_playlist.core import entity_names, recommendation_outputs as files
from yt_playlist.core import saved_preparation_files as recovery
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.preparation import saved_outputs as preparation, saved_output_publication as publication
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Saved file recovery does not construct the HTTP lifecycle."""


@pytest.fixture
def installed(tmp_path, _bounded_installed_schema):
    with _bounded_installed_schema(79).open(tmp_path/'installed.db') as store:
        yield store, 79


@pytest.fixture(scope='module')
def saved_templates(staged_custody_templates, upgraded_custody_inputs, _bounded_installed_schema):
    native_template = None

    def native(target, *, phase='captured'):
        completed = template('selected')
        return upgraded_custody_inputs(79, target, 'saved-recovery-native',
            lambda _store: completed.result, prepared=completed, phase=phase)

    def template(kind):
        nonlocal native_template
        if kind == 'native-precedence':
            if native_template is None:
                converted = native(95, phase='converted')
                source = converted.source
                source.conn.execute('PRAGMA query_only=OFF')
                entity_names.predecessor_reads(source.conn)
                source._snapshot_revisions = Revisions(source.conn)
                source._bind_repositories()
                native_template = capture_template(source, converted.result)
            return native_template
        return staged_custody_templates(79, 'saved-recovery-'+kind, lambda store: _prepare(store, kind),
            completed=_bounded_installed_schema(79), admit=False)
    template.native = native
    template.release = lambda: upgraded_custody_inputs.release(79, 'saved-recovery-native')
    return template


def test_saved_recovery_schema_input_keeps_real_history_and_private_guards(_bounded_installed_schema):
    assert_upgrade_phase(_bounded_installed_schema(79), 79)


@pytest.mark.parametrize('kind', ['selected', 'unfinished'])
def test_saved_recovery_input_keeps_exact_originals_and_distinct_selection(saved_templates, kind):
    template = saved_templates(kind)
    assert_upgrade_phase(template, 79)
    assert _originals(template.source.conn)
    selected = publication.selected(template.source.conn, 'radio_seed_offer')
    assert (selected is not None) is (kind == 'selected')


NATIVE_PHASES = [item for item in custody_upgrade_cases((79,)) if item[1] <= 95] + [
    (79, 95, 'input'), (79, 95, 'converted')]


@pytest.mark.parametrize('version,target,phase', NATIVE_PHASES)
def test_saved_recovery_native_phase_keeps_history_and_exact_originals(saved_templates, version, target, phase):
    template = saved_templates.native(target, phase=phase)
    assert_upgrade_phase(template, custody_predecessor(version, target) if phase == 'input' else target)
    assert _originals(template.source.conn)


def test_saved_recovery_native_input_keeps_real_private_revision_guards(saved_templates):
    template = saved_templates('native-precedence')
    assert_upgrade_phase(template, 95)
    assert template.source._snapshot_revisions.conn is template.source.conn
    assert _originals(template.source.conn)


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


@pytest.fixture(scope='module')
def recovery_exports(saved_templates, tmp_path_factory):
    reports = {}

    def get(kind):
        if kind not in reports:
            template = saved_templates(kind)
            producer, expected = template.source, template.result
            before = list(producer.conn.iterdump())
            originals, custody = _originals(producer.conn), _custody(producer.conn)
            root = recovery.directory(producer.conn)
            hashes = _hashes(root)
            selected = {source: publication.selected(producer.conn, source) for source in expected}
            report = fixture_files.snapshot_library(producer._db_path,
                tmp_path_factory.mktemp('saved-recovery-export')/'recovered')
            assert list(producer.conn.iterdump()) == before
            assert _hashes(root) == hashes
            assert 'saved_preparation' in report['manifests']
            with closing(sqlite3.connect(report['snapshot'])) as backup:
                assert list(backup.iterdump()) == before
            reports[kind] = SimpleNamespace(producer=producer, expected=expected, before=before,
                originals=originals, custody=custody, root=root, hashes=hashes, selected=selected, report=report)
        return reports[kind]
    return get


@pytest.mark.parametrize('kind', ['selected', 'unfinished', 'native-precedence'])
def test_saved_recovery_export_preserves_exact_source_and_snapshot(recovery_exports, kind):
    state = recovery_exports(kind)
    assert state.report['snapshot']
    assert state.originals


@pytest.mark.parametrize('kind', ['selected', 'unfinished', 'native-precedence'])
@pytest.mark.parametrize('original', [False, True], ids=['restored', 'snapshot'])
def test_recovered_saved_outputs_own_exact_history_and_continue_without_source(
        recovery_exports, tmp_path, monkeypatch, kind, original):
    state = recovery_exports(kind)
    producer, expected, before = state.producer, state.expected, state.before
    originals, custody, root, hashes = state.originals, state.custody, state.root, state.hashes
    selected, report = state.selected, state.report
    with closing(sqlite3.connect(report['snapshot'])) as backup:
        if original:
            path = tmp_path / 'restored.db'
            with closing(sqlite3.connect(path)) as destination:
                backup.backup(destination)
        else:
            path = Path(report['restored'])
    candidate = Store(path)
    hidden = root.with_name(root.name + '-unavailable')
    root.rename(hidden)
    try:
        if original:
            fixture_files.restore_files(candidate.conn, report['manifests'])
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
            fixture_files.snapshot_library(store._db_path, tmp_path / 'failed')
        assert not (tmp_path / 'failed' / 'restored.db').exists()
        assert list(store.conn.iterdump()) == before
    finally:
        hidden.rename(path)


def test_saved_recovery_native_inputs_close_after_every_consumer(saved_templates):
    for source in saved_templates.release():
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            source.conn.execute('SELECT 1')
