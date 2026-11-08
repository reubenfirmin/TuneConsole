"""Published output families share one populated, real historical upgrade route."""
import sqlite3
from unittest.mock import Mock

import pytest

from tests.core import test_album_note_publication as notes
from tests.core import test_checkpoint_publication as checkpoints
from tests.core import test_saved_output_publication as saved
from tests.core import test_album_share_publication as shares
from tests.core import test_listening_publication as listening
from tests.prepared_fixture_support import assert_upgrade_phase, capture_template, custody_predecessor, custody_upgrade_cases
from tests.store_fixture_support import reuse_reporting_reader
from yt_playlist.core import schema_inspection
from yt_playlist.core.migrations import MigrationError, regular, v103_entity_names
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """This SQL/file journey has no HTTP lifecycle."""


@pytest.fixture(scope='module')
def output_documents(_bounded_installed_schema, tmp_path_factory):
    path = tmp_path_factory.mktemp('published-output-input')/'source.db'
    with _bounded_installed_schema(79).open(path) as store, pytest.MonkeyPatch.context() as patch:
        with reuse_reporting_reader(store, patch), schema_inspection.reuse_tables(store.conn):
            checkpoints.seed(store)
            note_raw = notes.seed(store)
            sources, _ = saved.seed(store)
            shares.complete_source(store)
            documents = listening.seed(store)
            yield store, dict(note_raw=note_raw, sources=sources, listening=documents)


def test_output_document_inputs_keep_exact_sources_before_any_publication(output_documents):
    store, values = output_documents
    assert store.settings.get_setting('radio_seed_offer') == values['sources']['radio_seed_offer']
    assert store.rec_proposals._proposal_json(notes.preparation.SURFACE) == values['note_raw']
    for surface, raw in values['listening'].items():
        assert store.rec_proposals._proposal_json(surface) == raw
    assert store.collection.is_album_tracklist_complete('release')
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def checkpoint_input(output_documents):
    store, _ = output_documents
    for name in checkpoints.preparation.JOBS:
        checkpoints.publication.publish(store.conn, name, revision=checkpoints.prepare(store, name))
    return output_documents


def test_checkpoint_input_keeps_each_real_published_selection(checkpoint_input):
    store, _ = checkpoint_input
    assert all(checkpoints.publication.selected(store.conn, name) is not None
               for name in checkpoints.preparation.JOBS)


@pytest.fixture(scope='module')
def note_input(checkpoint_input):
    store, values = checkpoint_input
    notes.publication.publish(store.conn, revision=notes.prepared(store.conn))
    values['note_header'] = dict(notes.header(store.conn))
    return checkpoint_input


def test_note_input_keeps_the_published_exact_multibyte_original(note_input):
    store, values = note_input
    assert notes.publication.selected(store.conn) is not None
    assert notes.preparation.read_original(values['note_header']) == values['note_raw']


@pytest.fixture(scope='module')
def saved_input(note_input):
    store, values = note_input
    for name in saved.preparation.JOBS:
        saved.publish(store.conn, name)
    values['saved_headers'] = {source: dict(saved.head(store.conn, source)) for source in values['sources']}
    return note_input


def test_saved_input_keeps_both_real_published_outputs_and_exact_files(saved_input):
    store, values = saved_input
    for source, raw in values['sources'].items():
        assert saved.publication.selected(store.conn, source) is not None
        assert saved.preparation.read_original(values['saved_headers'][source]) == raw


@pytest.fixture(scope='module')
def share_input(saved_input):
    store, values = saved_input
    selected = shares.stage(store.conn)
    shares.publication.OWNER.publish(store.conn, stage=selected)
    values['share_stage'] = selected
    values['share_original'] = shares.publication.OWNER.value(store.conn, selected, 'release')
    return saved_input


def test_share_input_keeps_selected_ordered_duplicates_and_the_unresolved_subject(share_input):
    store, values = share_input
    assert shares.publication.OWNER.selected(store.conn) == values['share_stage']
    assert store.collection.cached_album_share('release') == {'title':'Frozen 東京', 'video_ids':['one','missing','one']}


@pytest.fixture(scope='module')
def listening_input(share_input):
    store, values = share_input
    listening.prepared(store.conn)
    listening.publication.OWNER.publish(store.conn, stage=listening.mapped(store.conn).id)
    values['listening_headers'] = {surface: dict(listening.head(store.conn, surface)) for surface in values['listening']}
    return share_input


def test_listening_input_keeps_selected_edition_manifest_and_all_four_exact_chunks(listening_input):
    store, values = listening_input
    assert listening.publication.OWNER.selected(store.conn) is not None
    for surface, raw in values['listening'].items():
        assert listening.preparation.read_original(values['listening_headers'][surface]) == raw


@pytest.fixture(scope='module')
def output_upgrade_inputs(upgraded_custody_inputs, listening_input):
    store, values = listening_input
    completed = capture_template(store, values)

    def retain_published_input(_store):
        return completed.result

    def get(target, **options):
        return upgraded_custody_inputs(79, target, 'published-output-custody', retain_published_input,
            prepared=completed, **options)
    get.release = lambda: upgraded_custody_inputs.release(79, 'published-output-custody')
    get.entity_programs = None
    return get


def assert_originals(template):
    values = template.result
    assert notes.preparation.read_original(values['note_header']) == values['note_raw']
    for source, header in values['saved_headers'].items():
        assert saved.preparation.read_original(header) == values['sources'][source]
    for surface, header in values['listening_headers'].items():
        assert listening.preparation.read_original(header) == values['listening'][surface]


def test_output_upgrade_schema_input_keeps_the_registered_installed_contract(_bounded_installed_schema):
    assert_upgrade_phase(_bounded_installed_schema(79), 79)


def test_output_upgrade_input_keeps_all_published_files_and_selections(output_upgrade_inputs):
    template = output_upgrade_inputs(79)
    assert_upgrade_phase(template, 79)
    assert_originals(template)
    conn = template.source.conn
    assert all(checkpoints.publication.selected(conn, name) is not None
               for name in checkpoints.preparation.JOBS)
    assert notes.publication.selected(conn) is not None
    assert all(saved.publication.selected(conn, source) is not None for source in template.result['sources'])


UPGRADE_CASES = [case for item in custody_upgrade_cases((79,))
                 for case in ([item, (79, 103, 'programs')] if item == (79, 103, 'converted') else [item])]


@pytest.mark.parametrize('version,target,phase', UPGRADE_CASES)
def test_output_upgrade_phase_keeps_real_history_and_all_original_files(output_upgrade_inputs, version, target, phase):
    if target == 103 and phase == 'converted':
        # The actual rename and actual dependent-program restoration have
        # separate owners, rather than repeating both for every publication.
        restore, guards = Mock(), Mock()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(v103_entity_names, 'restore_programs', restore)
            patch.setattr(regular, 'after_owner_upgrade', guards)
            template = output_upgrade_inputs(target, phase=phase)
        restore.assert_called_once()
        guards.assert_called_once_with(template.source.conn)
        output_upgrade_inputs.entity_programs = restore.call_args
    elif phase == 'programs':
        template = output_upgrade_inputs(target, phase='converted')
        conn = template.source.conn
        conn.execute('PRAGMA query_only=OFF')
        try:
            with conn:
                conn.execute('BEGIN')
                programs = output_upgrade_inputs.entity_programs
                v103_entity_names.restore_programs(*programs.args, **programs.kwargs)
                regular.after_owner_upgrade(conn)
        finally:
            conn.execute('PRAGMA query_only=ON')
    else:
        template = output_upgrade_inputs(target, phase=phase)
    assert_upgrade_phase(template, custody_predecessor(version, target) if phase == 'input' else target)
    assert_originals(template)


def test_output_upgrade_serving_input_keeps_the_actual_current_revision_guards(output_upgrade_inputs):
    template = output_upgrade_inputs(107)
    assert_upgrade_phase(template, 107)
    assert template.source._snapshot_revisions.conn is template.source.conn
    assert_originals(template)


def test_checkpoint_native_upgrade_keeps_route_and_retires_old_selections(output_upgrade_inputs):
    conn = output_upgrade_inputs(107).source.conn
    assert all(checkpoints.publication.selected(conn, name) is None for name in checkpoints.preparation.JOBS)
    regular.validate(conn)


def test_album_note_native_upgrade_keeps_files_and_rejects_a_missing_guard(output_upgrade_inputs, tmp_path):
    template = output_upgrade_inputs(107)
    with template.open(tmp_path/'notes.db') as store, schema_inspection.reuse_tables(store.conn):
        path = store._db_path
    reopened = Store(path)
    try:
        with schema_inspection.reuse_tables(reopened.conn):
            reopened.init_schema(defer_metadata=True)
            assert notes.publication.selected(reopened.conn) is None
            assert reopened.rec_proposals.get_proposals('album_notes') == notes.NOTES
            assert notes.preparation.read_original(template.result['note_header']) == template.result['note_raw']
            regular.validate(reopened.conn)
            with reopened.conn:
                reopened.conn.execute('DROP TRIGGER prepared_album_note_files_published_delete')
            with pytest.raises(MigrationError, match='publication object'):
                regular.validate(reopened.conn)
    finally:
        reopened.conn.close()


def test_saved_native_upgrade_keeps_exact_published_custody_and_prefers_native_outputs(output_upgrade_inputs):
    template = output_upgrade_inputs(107)
    conn = template.source.conn
    regular.validate(conn)
    for source, raw in template.result['sources'].items():
        assert saved.publication.selected(conn, source) is None
        assert saved.outputs.get(conn, source) == raw
    assert_originals(template)
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


def test_album_share_native_upgrade_keeps_map_custody_and_rejects_a_missing_guard(output_upgrade_inputs, tmp_path):
    template = output_upgrade_inputs(107)
    with template.open(tmp_path/'shares.db') as source:
        path = source._db_path
    reopened = Store(path)
    try:
        with schema_inspection.reuse_tables(reopened.conn):
            reopened.init_schema(defer_metadata=True)
            assert shares.album_tracklists.available(reopened.conn)
            assert shares.publication.OWNER.selected(reopened.conn) is None
            assert reopened.collection.cached_album_share('release') == {'title':'Frozen 東京', 'video_ids':['one','missing','one']}
            assert shares.publication.OWNER.value(reopened.conn, template.result['share_stage'], 'release') == template.result['share_original']
            regular.validate(reopened.conn)
            with reopened.conn:
                reopened.conn.execute('DROP TRIGGER prepared_album_share_items_mapped_delete')
            with pytest.raises(MigrationError, match='publication object'):
                regular.validate(reopened.conn)
    finally:
        reopened.conn.close()


def test_listening_native_upgrade_keeps_exact_files_and_rejects_a_missing_guard(output_upgrade_inputs, tmp_path):
    template = output_upgrade_inputs(107)
    with template.open(tmp_path/'listening.db') as source:
        path = source._db_path
    reopened = Store(path)
    try:
        with schema_inspection.reuse_tables(reopened.conn):
            reopened.init_schema(defer_metadata=True)
            assert listening.publication.OWNER.selected(reopened.conn) is None
            for surface, raw in template.result['listening'].items():
                assert reopened.rec_proposals._proposal_json(surface) == raw
            regular.validate(reopened.conn)
            with reopened.conn:
                reopened.conn.execute('DROP TRIGGER '+listening.preparation.HEAD+'_mapped_delete')
            with pytest.raises(MigrationError, match='Incompatible mapped publication object'):
                regular.validate(reopened.conn)
    finally:
        reopened.conn.close()


def test_output_upgrade_input_closes_all_original_and_converted_owners(output_upgrade_inputs):
    sources = output_upgrade_inputs.release()
    assert len(sources) == len(custody_upgrade_cases((79,))) // 2 + 1
    for source in sources:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            source.conn.execute('SELECT 1')
