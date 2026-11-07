"""Physical locator retirement has independent custody and format gates."""
from contextlib import ExitStack, closing
from pathlib import Path
import shutil
import sqlite3
import time

import pytest

from tests.core.test_metadata_runtime_preparation import seeded, run
from tests.prepared_fixture_support import capture_template, roots
from tests.store_fixture_support import initialize_consumer
from tests.metadata_provenance_support import metadata_provenance
from yt_playlist.core import catalog, fao, metadata_runtime_files as files
from yt_playlist.core.db_rebuild import build
from yt_playlist.core.metadata_deliveries import Delivery, record_many
from yt_playlist.core.migrations import current
from yt_playlist.core.preparation import metadata_locators as retirement, metadata_runtime as preparation
from yt_playlist.core.store import Store


def schema(conn):
    return list(map(tuple, conn.execute('SELECT type,name,sql FROM sqlite_schema ORDER BY name')))


@pytest.fixture(scope='module')
def locator_templates(tmp_path_factory, _pristine_current_schema):
    root = tmp_path_factory.mktemp('metadata-locator-templates')
    with ExitStack() as resources:
        (root / 'source').mkdir()
        source = Store(root / 'source' / 'library.db')
        resources.callback(source.conn.close)
        initialize_consumer(source, _pristine_current_schema)
        _, entry = seeded(source)
        progress = root / 'progress.json'
        run(source, progress)
        preparation.activate(source.conn, progress)
        history = list(map(tuple, source.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        with source.conn:
            source.conn.execute('BEGIN')
            manifest = Path(files.export(source.conn, root / 'recovery'))
        (root / 'candidate').mkdir()
        path = root / 'candidate' / 'library.db'
        build.build(source._db_path, path)
        target = Store(path)
        resources.callback(target.conn.close)
        files.restore(target.conn, manifest)
        target.init_schema()
        yield (capture_template(source, entry), capture_template(target, history), manifest)


@pytest.fixture
def candidate(locator_templates, file_backed_store, tmp_path, _pristine_current_schema):
    source_template, target_template, original_manifest = locator_templates
    source = file_backed_store
    entry = source_template.restore(source)
    (tmp_path / 'candidate').mkdir()
    target = Store(tmp_path / 'candidate' / 'library.db')
    try:
        initialize_consumer(target, _pristine_current_schema)
        history = target_template.restore(target)
        # The recovery manifest and its members are independent custody, outside
        # either database's owner roots. Every consumer can damage its own copy.
        backup = tmp_path / 'recovery'
        shutil.copytree(original_manifest.parent, backup)
        yield source, target, entry, backup / original_manifest.name, history
    finally:
        target.conn.close()


def test_inactive_and_populated_formats_keep_installed_locators(file_backed_store, tmp_path):
    store, _entry = seeded(file_backed_store)
    try:
        before = schema(store.conn)
        with pytest.raises(ValueError, match='Activate verified'):
            retirement.retire(store.conn, tmp_path / 'absent.json')
        assert schema(store.conn) == before
        progress = tmp_path / 'progress.json'
        run(store, progress)
        preparation.activate(store.conn, progress)
        before = schema(store.conn)
        with pytest.raises(ValueError, match='populated installed metadata'):
            retirement.retire(store.conn, tmp_path / 'absent.json')
        assert schema(store.conn) == before
        assert 'raw_filename' in {row[1] for row in store.conn.execute('PRAGMA table_info(metadata_deliveries)')}
    finally:
        store.conn.close()


def test_retirement_removes_physical_fields_and_preserves_reopen_writes_recovery(candidate, tmp_path):
    source, target, entry, manifest, history = candidate
    original = schema(source.conn)
    decisions = metadata_provenance(target, entry)['decisions']
    detail = target.enrichment_reports.enrichment_conflict_detail(entry, 'genre')
    assert decisions and detail['options']
    result = retirement.retire(target.conn, manifest)
    assert result['verified_recovery_members'] > 0
    assert not retirement.COLUMNS.intersection(row[1] for row in target.conn.execute('PRAGMA table_info(metadata_deliveries)'))
    assert not retirement.REMOVED.intersection(row[0] for row in target.conn.execute('SELECT name FROM sqlite_schema'))
    assert list(map(tuple, target.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
    assert schema(source.conn) == original
    assert retirement.retire(target.conn, manifest)['already_retired']
    with pytest.raises(ValueError, match='writes are retired'):
        with target.conn:
            target.conn.execute('BEGIN')
            record_many(target.conn, [Delivery('a', 'bad', 'lookup', None, time.time())])
    target.init_schema()
    assert metadata_provenance(target, entry)['decisions'] == decisions
    assert target.enrichment_reports.enrichment_conflict_detail(entry, 'genre') == detail
    fields = {'bpm': 145}
    captured, _ = target.enrichment_state.capture_provider_result(entry, 'continued', fields, run_id='new')
    target.enrichment.apply_provider_fields(entry, 'continued', fields, providers=('continued',), delivery_id=captured)
    target.enrichment_state.record_enrichment_attempt('song|artist', 'continued', fields,
        video_id='video', run_id='new', delivery_id=captured)
    with target.conn:
        catalog.choose_field(target.conn, entry, 'bpm', 140)
        catalog.withdraw_choice(target.conn, entry, 'bpm')
    assert target.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0] == 145
    # Current domain exporters must still restore the retired installed format.
    from tests import file_custody_support as custody
    report = custody.snapshot_library(target._db_path, tmp_path / 'recovered')
    restored = Store(Path(report['restored']))
    try:
        restored.init_schema()
        assert retirement.retired(restored.conn)
        assert metadata_provenance(restored, entry)['decisions']
        assert not restored.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        restored.conn.close()


@pytest.mark.parametrize('retired', [False, True])
def test_metadata_collection_preserves_installed_owners_and_retired_snapshot_fences(candidate, retired):
    from yt_playlist.core import metadata_delivery_files
    from yt_playlist.core.preparation.managed_files import Orphans

    source, target, _entry, manifest, _history = candidate
    store = target if retired else source
    if retired:
        retirement.retire(store.conn, manifest)
    owned = list(metadata_delivery_files.references(store.conn))
    assert bool(owned) is not retired
    before = {row[1]: fao.read(row[1]).bytes for row in owned}
    root = metadata_delivery_files.directory(store.conn)
    old, young = root / 'unowned-old.json', root / 'unowned-young.json'
    fao.write(old, b'{"unowned":"old"}')
    now = time.time() + 31 * 86400
    age = fao.read(old).info
    from dataclasses import replace
    fao.write(young, fao.Content(b'{"unowned":"young"}', metadata=replace(age, ctime_ns=int(now * 1000000000.0))))
    work = Orphans(store.conn)
    try:
        with store.read_view():
            for _ in range(24):
                assert work.step(store, now, limit=1) == 0
            assert old.exists()
        # Rebuild traversal as a restarted maintenance worker would.
        work.close()
        work = Orphans(store.conn)
        for _ in range(64):
            work.step(store, now, limit=1)
            if not old.exists():
                break
        assert not old.exists()
        assert young.exists()
        assert {path: fao.read(path).bytes for path in before} == before
        assert all(metadata_delivery_files.owns_file(store.conn, path) for path in before)
        assert not metadata_delivery_files.owns_file(store.conn, old)
        # Recovery and source bytes are separate custody, never collector roots.
        assert manifest.exists()
        assert files.verify_backup(target.conn, manifest)
    finally:
        work.close()


@pytest.mark.parametrize('definition', [
    'CREATE VIEW additional_locator_reader AS SELECT raw_filename FROM metadata_deliveries',
    'CREATE TRIGGER additional_delivery_guard BEFORE DELETE ON metadata_deliveries BEGIN SELECT 1; END',
])
def test_unreviewed_dependencies_keep_exact_installed_schema(candidate, definition):
    _source, target, _entry, manifest, _history = candidate
    with target.conn:
        target.conn.execute(definition)
    before = schema(target.conn)
    with pytest.raises(ValueError, match='new dependency audit'):
        retirement.retire(target.conn, manifest)
    assert schema(target.conn) == before
    assert not retirement.retired(target.conn)
    assert not target.conn.in_transaction


@pytest.mark.parametrize('failure', ['missing-backup', 'changed-backup', 'ddl', 'concurrent-write'])
def test_failed_retirement_keeps_exact_installed_schema_and_can_retry(candidate, monkeypatch, failure):
    _source, target, _entry, manifest, _history = candidate
    before = schema(target.conn)
    verify = files.verify_backup
    if failure in ('missing-backup', 'changed-backup'):
        encoded = fao.read(manifest).bytes
        if failure == 'missing-backup':
            fao.delete(manifest)
        else:
            import json
            document = json.loads(encoded)
            document['entries'][0]['sha256'] = '0' * 64
            from yt_playlist.core.evidence_ledger import canonical
            fao.write(manifest, canonical(document))
    elif failure == 'ddl':
        def interrupt():
            if retirement.retired(target.conn):
                raise InterruptedError('after retirement DDL')
        monkeypatch.setattr(retirement, 'check_cancellable_work', interrupt)
    else:
        def changed(conn, path):
            result = verify(conn, path)
            with closing(sqlite3.connect(target._db_path)) as other:
                other.execute("INSERT INTO settings VALUES ('concurrent-retirement','changed')")
                other.commit()
            return result
        monkeypatch.setattr(files, 'verify_backup', changed)
    with pytest.raises((ValueError, OSError), match='verification|changed|DDL|No such file'):
        retirement.retire(target.conn, manifest)
    assert schema(target.conn) == before
    assert not retirement.retired(target.conn)
    assert not target.conn.in_transaction
    monkeypatch.undo()
    if failure in ('missing-backup', 'changed-backup'):
        fao.write(manifest, encoded)
    assert retirement.retire(target.conn, manifest)['retired']


def test_candidate_owns_private_sources_targets_and_recovery_members(candidate, locator_templates):
    source, target, entry, manifest, history = candidate
    source_template, target_template, original_manifest = locator_templates
    assert source._db_path.parent != target._db_path.parent
    for consumer, template in ((source, source_template), (target, target_template)):
        assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
        assert list(map(tuple, consumer.conn.execute(
            'SELECT * FROM schema_migrations ORDER BY version'))) == history
        assert not consumer.conn.execute('PRAGMA foreign_key_check').fetchone()
        owned = roots(consumer.conn)
        copied = 0
        for name, directory in template.files.items():
            assert owned[name] != directory, name
            for path in directory.rglob('*'):
                if not path.is_file():
                    continue
                private = owned[name] / path.relative_to(directory)
                assert private.read_bytes() == path.read_bytes()
                assert (private.stat().st_dev, private.stat().st_ino) != (path.stat().st_dev, path.stat().st_ino)
                copied += 1
        assert copied
    assert files.verify_backup(target.conn, manifest)
    copied = 0
    for path in original_manifest.parent.rglob('*'):
        if not path.is_file():
            continue
        private = manifest.parent / path.relative_to(original_manifest.parent)
        assert private.read_bytes() == path.read_bytes()
        assert (private.stat().st_dev, private.stat().st_ino) != (path.stat().st_dev, path.stat().st_ino)
        copied += 1
    assert copied > 1
    encoded = fao.read(original_manifest).bytes
    fao.write(manifest, b'private damaged recovery manifest')
    assert fao.read(original_manifest).bytes == encoded
    expected = metadata_provenance(target, entry)
    source.settings.set_setting('private-fixture', 'source consumer')
    directory = files.directory(source.conn)
    hidden = directory.with_name(directory.name + '-unavailable')
    directory.rename(hidden)
    try:
        assert metadata_provenance(target, entry) == expected
    finally:
        hidden.rename(directory)
    for consumer in (target, source_template.source, target_template.source):
        assert consumer.settings.get_setting('private-fixture') is None
