"""Separate real conversion, checkpoint, admission and compaction obligations."""
from contextlib import closing
from dataclasses import replace
import json
from itertools import groupby
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import pytest

from tests.core import test_db_rebuild_destination as evidence
from tests.db_rebuild_support import populate_catalog
from tests.genre_catalog_support import bound_catalog
from tests.prepared_fixture_support import capture_template, coordinates, roots
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core import destination_schema, vibe_versions
from yt_playlist.core.db_rebuild import build, destination, files, source
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.migrations import registry, runner
from yt_playlist.core.store import Store


@pytest.fixture(scope='module')
def captured_rebuild_source(tmp_path_factory):
    root = tmp_path_factory.mktemp('populated-rebuild')
    path = root / 'captured93.db'
    costs = {}
    started = time.perf_counter()
    with pytest.MonkeyPatch.context() as patch:
        # The vocabulary is a lower input. All catalog identities, metadata,
        # converters, file publications, validators and SQLite remain real.
        bound_catalog(patch, entries=(('house', 'House'), ('jazz', 'Jazz'),
                                     ('rock', 'Rock'), ('synth-pop', 'Synth Pop')))
        document = destination_schema.definition()
        seeds = [sql for sql in document['destination_seeds'] if not sql.startswith(
            ('INSERT INTO "genre_alias"', 'INSERT INTO "genre_catalog"', 'INSERT INTO "genre_family"'))]
        patch.setattr(destination_schema, 'definition', lambda: {**document, 'destination_seeds': seeds})
        with closing(sqlite3.connect(path)) as conn:
            build._configure(conn)
            install_numbered_input(conn, 93)
            with conn:
                populate_catalog(conn)
                vibe_versions.install(conn)
                conn.execute("INSERT INTO vibe_versions VALUES (23,'retained-unused-version')")
        costs['source_schema_and_catalog'] = time.perf_counter() - started
        started = time.perf_counter()
        draft, radio, recipe = evidence.seed_populated_decisions(path)
        discovery = evidence.seed_discovery_visibility(path)
        costs['populated_evidence'] = time.perf_counter() - started
        before = evidence.source_contents(path)
        yield SimpleNamespace(root=root, path=path, draft=draft, radio=radio, recipe=recipe,
                              discovery=discovery, before=before, costs=costs)


def test_captured_populated_source_is_admitted_without_writes(captured_rebuild_source, record_property):
    captured = captured_rebuild_source
    with closing(sqlite3.connect(captured.path.as_uri()+'?mode=ro', uri=True)) as reader:
        reader.execute('PRAGMA query_only=ON')
        assert source.admit(reader) == 93
        assert reader.total_changes == 0
        assert json.loads(reader.execute('SELECT state FROM road_trip_drafts WHERE recipe_id=7')
                          .fetchone()[0]) == captured.draft
        assert reader.execute('SELECT video_id FROM play_events WHERE id=92').fetchone()[0] == 'queued-only'
    assert evidence.source_contents(captured.path) == captured.before
    record_property('captured_source_costs', json.dumps(captured.costs))


@pytest.fixture(scope='module')
def captured_rebuild(captured_rebuild_source):
    captured = captured_rebuild_source
    metadata, continuing = evidence.seed_development_metadata(captured.path)
    evidence.seed_metadata_preparation(captured.path, active=False)
    journals = evidence.seed_journal_policy(captured.path)
    before = evidence.source_contents(captured.path)
    with closing(sqlite3.connect(':memory:')) as empty:
        initialize = destination.initialize
        def capture_initialized(conn):
            initialize(conn)
            # for_source already builds the genuine empty destination for
            # mapping validation. Retain that exact SQLite snapshot rather
            # than bootstrap the same lower input a second time.
            empty.deserialize(conn.serialize())
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(destination, 'initialize', capture_initialized)
            contract = destination.for_source(captured.path)
        empty.execute('PRAGMA query_only=ON')
        yield SimpleNamespace(**{**vars(captured), 'before': before}, contract=contract, empty=empty,
                              metadata=metadata, continuing=continuing, journals=journals)


def test_registered_rebuild_initializes_and_orders_populated_owners(captured_rebuild, record_property):
    captured = captured_rebuild
    record_property('captured_fixture_costs', json.dumps(captured.costs))
    names = [stage.name for stage in captured.contract.stages]
    assert len(names) == len(set(names))
    assert [name for name in names if name in {
        'copy:vibe_versions', 'copy:measurements', 'copy:recording_vibes'}] == [
            'copy:vibe_versions', 'copy:measurements', 'copy:recording_vibes']
    assert names.index('road_trip_drafts') < names.index('metadata:choices') < names.index('guards')
    assert names.index('guards') < names.index('mixes:choices') < names.index('mixes:groups')
    assert captured.contract.compact
    assert runner.history(captured.empty, registry.MIGRATIONS) == registry.MIGRATIONS[-1].version
    assert captured.empty.execute('SELECT count(*) FROM database_rebuild_source_history').fetchone()[0] == 0
    assert evidence.source_contents(captured.path) == captured.before


def run_registered_stages(reader, store, stages):
    for repositories, owners in groupby(stages, key=lambda stage: stage.repositories):
        if repositories:
            # Repository stages open real reporting readers. Publish the
            # completed SQL/schema prefix before opening those readers.
            assert store.conn.execute('PRAGMA main.journal_mode=WAL').fetchone()[0] == 'wal'
        with store.conn.atomic():
            for stage in owners:
                cursor = None
                while True:
                    batch = stage.copy(reader, store if repositories else store.conn,
                                       cursor, stage.batch_size)
                    assert isinstance(batch, build.Batch)
                    if batch.done:
                        break
                    assert batch.cursor != cursor, stage.name
                    cursor = batch.cursor


@pytest.fixture(scope='module')
def copied_rebuild(captured_rebuild):
    captured = captured_rebuild
    store = Store(captured.root / 'copied.db')
    try:
        captured.empty.backup(store.conn)
        store.conn.execute('PRAGMA foreign_keys=OFF')
        store.conn.execute('ATTACH DATABASE ? AS rebuild_source', (captured.path.as_uri()+'?mode=ro',))
        end = next(index for index, stage in enumerate(captured.contract.stages)
                   if stage.name == 'indexes') + 1
        with closing(sqlite3.connect(captured.path.as_uri()+'?mode=ro', uri=True)) as reader:
            build._configure(reader)
            reader.row_factory = sqlite3.Row
            run_registered_stages(reader, store, captured.contract.stages[:end])
        store.conn.execute('PRAGMA query_only=ON')
        yield store, end
    finally:
        store.conn.close()


def test_registered_copies_preserve_catalog_identities_and_version_dictionary(copied_rebuild, captured_rebuild):
    store, _end = copied_rebuild
    assert [tuple(row) for row in store.conn.execute('SELECT id,label FROM accounts ORDER BY id')] == [
        (11, 'Main'), (29, 'Other account')]
    assert [tuple(row) for row in store.conn.execute('SELECT id,account_id FROM playlists ORDER BY id')] == [
        (41, 11), (73, 29)]
    assert [row[0] for row in store.conn.execute('SELECT video_id FROM playlist_items '
        'WHERE playlist_id=41 ORDER BY position')] == ['video-19', 'video-3', 'video-8']
    evidence.assert_versioned_scores(store.conn)
    assert store.conn.execute('SELECT value FROM vibe_versions WHERE id=23').fetchone()[0] == 'retained-unused-version'
    assert not store.conn.execute('SELECT 1 FROM metadata_runtime_choices WHERE id=999').fetchone()
    assert evidence.source_contents(captured_rebuild.path) == captured_rebuild.before


@pytest.fixture(scope='module')
def converted_rebuild(copied_rebuild, captured_rebuild):
    copied, end = copied_rebuild
    captured = captured_rebuild
    store = Store(captured.root / 'converted.db')
    try:
        copied.conn.backup(store.conn)
        store.conn.execute('PRAGMA foreign_keys=OFF')
        store._schema_compatibility = False
        store._rebuild_source_version = None
        store._schema_recovery_pending = False
        store._bind_repositories()
        files.borrow(captured.path, store._db_path)
        store.conn.execute('ATTACH DATABASE ? AS rebuild_source', (captured.path.as_uri()+'?mode=ro',))
        with closing(sqlite3.connect(captured.path.as_uri()+'?mode=ro', uri=True)) as reader:
            build._configure(reader)
            reader.row_factory = sqlite3.Row
            # The copied catalog has its own real owner above. Here execute
            # every subsequent registered domain converter and publication.
            # Atomic checkpoint commits have separate resume owners below.
            run_registered_stages(reader, store, captured.contract.stages[end:])
        store.conn.execute('PRAGMA foreign_keys=ON')
        store._bind_repositories()
        store._snapshot_revisions = Revisions(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
        owned = roots(store.conn)
        with closing(sqlite3.connect(captured.path)) as reader:
            borrowed = roots(reader)
        external = []
        for _table, _column, filename in coordinates(store.conn):
            path = Path(filename)
            if any(path.is_relative_to(root) for root in owned.values()):
                continue
            domain, root = next((name, root) for name, root in borrowed.items() if path.is_relative_to(root))
            external.append((filename, domain, path.relative_to(root)))
        yield capture_template(store, external)
    finally:
        store.conn.close()


@pytest.fixture
def imported_rebuild(converted_rebuild, tmp_path):
    store = Store(tmp_path / 'imported.db')
    try:
        converted_rebuild.restore(store, external_files=converted_rebuild.result)
        store._schema_compatibility = False
        store._rebuild_source_version = None
        store._schema_recovery_pending = False
        store._bind_repositories()
        assert store.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        yield store
    finally:
        store.conn.close()


@pytest.fixture(scope='module')
def compacted_rebuild(converted_rebuild, captured_rebuild):
    path = captured_rebuild.root / 'retained' / 'retained.db'
    path.parent.mkdir()
    store = Store(path)
    try:
        converted_rebuild.restore(store, external_files=converted_rebuild.result)
        output = path.with_name(path.name + '.compacted')
        report = build.build(path, output, now=captured_rebuild.path.stat().st_mtime)
        assert report['freelist_pages'] == 0
    finally:
        store.conn.close()
    # This is the actual construct() cutover: replace SQL at its existing
    # filename, keeping the immutable managed files at their installed roots.
    output.replace(path)
    store = Store(path)
    try:
        store._schema_compatibility = False
        store._rebuild_source_version = None
        store._schema_recovery_pending = False
        store._bind_repositories()
        yield capture_template(store, report)
    finally:
        store.conn.close()


@pytest.fixture
def writable_compacted_rebuild(compacted_rebuild, tmp_path):
    store = Store(tmp_path / 'continuing.db')
    try:
        compacted_rebuild.restore(store)
        store._schema_compatibility = False
        store._rebuild_source_version = None
        store._schema_recovery_pending = False
        store._bind_repositories()
        yield store
    finally:
        store.conn.close()


def assert_imported_decisions(store, captured):
    from yt_playlist.core import playlist_recipe_state, radio_storage, road_trip_draft_storage

    conn = store.conn
    assert conn.execute('SELECT max(version) FROM database_rebuild_source_history').fetchone()[0] == 93
    assert [tuple(row) for row in conn.execute('SELECT id,label FROM accounts ORDER BY id')] == [
        (11, 'Main'), (29, 'Other account')]
    assert [tuple(row) for row in conn.execute('SELECT id,account_id FROM playlists ORDER BY id')] == [(41, 11), (73, 29)]
    assert tuple(conn.execute('SELECT id,account_id,recording_id,membership_ref_id FROM play_events '
                             'WHERE id=91').fetchone()) == (91, 11, 8, None)
    assert [row[0] for row in conn.execute('SELECT video_id FROM playlist_items '
        'WHERE playlist_id=41 ORDER BY position')] == ['video-19', 'video-3', 'video-8']
    assert road_trip_draft_storage.get(conn, 7) == captured.draft
    assert radio_storage.load_published(conn)[0] == captured.radio
    assert playlist_recipe_state.load(conn, '@account:11:LM')['opaque'] == captured.recipe['opaque']
    assert playlist_recipe_state.load_canvas(conn, '@account:11:LM') == '{"edited":[3,2,1]}'
    assert tuple(conn.execute('SELECT recording_id,unresolved_subject_key FROM play_events WHERE id=92')
                 .fetchone()) == (None, '@unresolved-video:queued-only')
    assert tuple(conn.execute('SELECT phase,outcome FROM action_steps WHERE action_id=77')
                 .fetchone()) == ('uncertain', 'unknown')
    assert [tuple(row) for row in conn.execute('SELECT o.legacy_row_id,o.position,s.entry_id,o.recording_id '
        'FROM album_track_occurrences o LEFT JOIN album_occurrence_sources s ON s.occurrence_id=o.id '
        'ORDER BY o.legacy_row_id')] == [(401, 4, 119, 19), (403, 6, 119, 19), (410, 9, None, 3)]
    assert tuple(conn.execute('SELECT legacy_row_id,entry_id FROM album_item_observations')
                 .fetchone()) == (417, 108)
    assert conn.execute('SELECT status FROM album_tracklists').fetchone()[0] == 'partial'
    assert conn.execute('SELECT current_tracklist_id FROM albums WHERE id=25').fetchone()[0] is None


def test_registered_converters_preserve_populated_decisions(converted_rebuild, captured_rebuild):
    store, captured = converted_rebuild.source, captured_rebuild
    assert_imported_decisions(store, captured)
    after = evidence.source_contents(captured.path)
    assert {name: after.get(name) for name in captured.before} == captured.before


def test_registered_verifiers_preserve_populated_metadata_and_original_files(converted_rebuild, captured_rebuild):
    store, captured = converted_rebuild.source, captured_rebuild
    captured.discovery(store.conn)
    captured.metadata(store, compacted=False)
    captured.journals(store.conn)
    assert store.conn.execute('SELECT value FROM vibe_versions WHERE id=23').fetchone()[0] == 'retained-unused-version'
    assert not store.conn.execute('SELECT 1 FROM metadata_runtime_choices WHERE id=999').fetchone()
    assert {row[0] for row in store.conn.execute('SELECT impression_id FROM served_facts')} == {41, 42, 501, 502, 503}
    with closing(sqlite3.connect(captured.path.as_uri()+'?mode=ro', uri=True)) as reader:
        captured.contract.verify(reader, store.conn)
    after = evidence.source_contents(captured.path)
    assert {name: after.get(name) for name in captured.before} == captured.before


def test_registered_verifier_rejects_changed_discovery_offers(imported_rebuild, captured_rebuild):
    store, captured = imported_rebuild, captured_rebuild
    with closing(sqlite3.connect(captured.path.as_uri()+'?mode=ro', uri=True)) as reader:
        store.conn.execute('ATTACH DATABASE ? AS rebuild_source', (captured.path.as_uri()+'?mode=ro',))
        store.conn.execute('BEGIN')
        try:
            store.conn.execute('UPDATE discovery_legacy_offers SET offered_count=0 WHERE recording_id=8')
            with pytest.raises(ValueError, match='changed legacy discovery offers'):
                captured.contract.verify(reader, store.conn)
        finally:
            store.conn.rollback()
        captured.discovery(store.conn)


def test_populated_import_admits_and_reopens_without_changing_decisions(imported_rebuild, captured_rebuild):
    store = imported_rebuild
    store.init_schema()
    assert_imported_decisions(store, captured_rebuild)
    reopened = Store(store._db_path)
    try:
        reopened.init_schema()
        assert_imported_decisions(reopened, captured_rebuild)
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        reopened.conn.close()


def test_populated_import_compacts_retired_evidence_without_losing_native_facts(
        compacted_rebuild, captured_rebuild, record_property):
    record_property('compaction_phases', json.dumps(compacted_rebuild.result['phases']))
    compacted = compacted_rebuild.source
    assert_imported_decisions(compacted, captured_rebuild)
    captured_rebuild.metadata(compacted)
    captured_rebuild.discovery(compacted.conn)
    captured_rebuild.journals(compacted.conn)
    assert compacted.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0] == 0


def test_populated_import_keeps_metadata_receipts_and_accepts_continuing_writes(
        writable_compacted_rebuild, captured_rebuild):
    store = writable_compacted_rebuild
    captured_rebuild.continuing(store)
    store.settings.set_setting('post_rebuild_write', 'works')
    assert store.settings.get_setting('post_rebuild_write') == 'works'
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('owner', ['road_trip_drafts', 'metadata:choices'])
def test_populated_owner_resumes_its_committed_cursor_without_republishing(
        captured_rebuild, owner, tmp_path):
    from yt_playlist.core import metadata_runtime, metadata_runtime_files, road_trip_draft_storage

    captured = captured_rebuild
    prefix_end = next(index for index, stage in enumerate(captured.contract.stages)
                      if stage.name == 'indexes') + 1
    selected = next(stage for stage in captured.contract.stages if stage.name == owner)

    def initialize(conn):
        # Genuine canonical initialization and direct registered copies supply
        # only the lower parents/history. The selected converter and atomic
        # checkpoint/restart protocol remain the actual application code.
        captured.contract.initialize(conn)
        # construct's first initialization connection has URI parsing disabled.
        # Only the lower-input copy runs on this attachment; the actual resume
        # reader is opened read-only by construct, and source bytes are fenced.
        conn.execute('ATTACH DATABASE ? AS rebuild_source', (str(captured.path),))
        with closing(sqlite3.connect(captured.path.as_uri()+'?mode=ro', uri=True)) as reader:
            reader.row_factory = sqlite3.Row
            parents = (*captured.contract.stages[:prefix_end],
                       next(stage for stage in captured.contract.stages if stage.name == 'play_events'))
            for stage in parents:
                cursor = None
                while True:
                    batch = stage.copy(reader, conn, cursor, stage.batch_size)
                    if batch.done:
                        break
                    assert batch.cursor != cursor
                    cursor = batch.cursor

    def verify(_reader, conn):
        conn.row_factory = sqlite3.Row
        assert not [tuple(row) for row in conn.execute('PRAGMA foreign_key_check')]
        if owner == 'road_trip_drafts':
            assert road_trip_draft_storage.get(conn, 7) == captured.draft
            assert tuple(conn.execute('SELECT account_id,legacy_author_unknown,revision FROM road_trip_drafts')
                         .fetchone()) == (None, 1, 'held')
            assert [tuple(row) for row in conn.execute('SELECT selection_state,position,video_id '
                'FROM road_trip_draft_entries ORDER BY selection_state,position')] == [
                    ('excluded', 0, 'excluded'), ('picked', 0, 'selected'),
                    ('picked', 1, 'selected'), ('picked', 2, 'video-8')]
        else:
            row = conn.execute('SELECT * FROM metadata_runtime_choices').fetchone()
            assert (row['entry_id'], row['field'], row['revision'], row['number_value']) == (
                108, metadata_runtime.code('bpm'), 3, 130)
            history = [metadata_runtime_files.read(conn, 'choices', (row['id'],), revision)
                       for revision in range(1, 4)]
            assert [value['value'] for value in history] == [None, 130, None]
            assert history[-1]['source'] == 'unknown-clock' and history[-1]['at'] is None
            assert conn.execute('SELECT count(*) FROM metadata_runtime_choices').fetchone()[0] == 1
            assert not conn.execute('SELECT 1 FROM metadata_runtime_choices WHERE id=999').fetchone()

    contract = replace(captured.contract, initialize=initialize,
                       stages=(replace(selected, batch_size=1),), verify=verify, compact=False)
    output = tmp_path / 'resumed.db'
    progress = []

    def interrupt(phase, detail):
        if phase == 'copy':
            progress.append(detail)
            raise InterruptedError('committed domain cursor')

    with pytest.raises(InterruptedError, match='committed domain cursor'):
        build.construct(captured.path, output, contract, progress=interrupt)
    with closing(sqlite3.connect(output)) as conn:
        checkpoint = build._checkpoint(conn)
        assert checkpoint['stage'] == 0
        assert checkpoint['cursor'] == ([7] if owner == 'road_trip_drafts' else [108, 'bpm'])
        assert checkpoint['rows'] == {owner: 1}
        verify(None, conn)
    bodies = {name: digest for name, digest in evidence.source_contents(output).items() if name != str(output)}
    report = build.construct(captured.path, output, contract,
                             progress=lambda phase, detail: progress.append(detail) if phase == 'copy' else None)
    assert report['rows'] == {owner: 1}
    assert [detail['rows'] for detail in progress] == [1, 1]
    assert progress[-1]['cursor'] is None
    with closing(sqlite3.connect(output)) as conn:
        assert not conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (build.CHECKPOINT,)).fetchone()
        verify(None, conn)
    after = evidence.source_contents(output)
    assert {name: digest for name, digest in after.items() if name != str(output)} == bodies
    original = evidence.source_contents(captured.path)
    assert {name: original.get(name) for name in captured.before} == captured.before
