"""Collection archive families share their real populated native upgrade."""
import json
import sqlite3
from unittest.mock import Mock

import pytest

from tests.core import test_collection_document_publication as documents
from tests.core import test_collection_feedback_publication as feedback
from tests.core import test_import_publication as imports
from tests.core import test_taste_control_publication as taste
from tests.prepared_fixture_support import assert_upgrade_phase, capture_template, custody_predecessor, custody_upgrade_cases
from tests.store_fixture_support import reuse_reporting_reader
from yt_playlist.core import collection_recovery, schema_inspection
from yt_playlist.core.migrations import MigrationError, regular, v103_entity_names


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL/file checks do not start the HTTP lifecycle."""


def test_collection_archive_schema_input_keeps_real_history_and_private_guards(_bounded_installed_schema):
    assert_upgrade_phase(_bounded_installed_schema(93), 93)


@pytest.fixture(scope='module')
def archive_documents(_bounded_installed_schema, tmp_path_factory):
    path = tmp_path_factory.mktemp('collection-archives')/'source.db'
    with _bounded_installed_schema(93).open(path) as store, pytest.MonkeyPatch.context() as patch:
        with reuse_reporting_reader(store, patch), schema_inspection.reuse_tables(store.conn):
            feedback_originals = feedback.seed(store)
            taste_values, taste_originals = taste.seed(store)
            import_value, import_raw, recipe, canvas = documents.seed(store, master=False)
            yield store, dict(feedback_originals=feedback_originals, taste_values=taste_values,
                taste_originals=taste_originals, import_value=import_value, import_raw=import_raw,
                recipe=recipe, canvas=canvas)


def test_collection_archive_sources_keep_exact_multibyte_documents(archive_documents):
    store, values = archive_documents
    for key, raw in (values['feedback_originals'] | values['taste_originals']).items():
        assert store.settings.get_setting(key) == raw
    assert store.settings.get_setting(imports.spotify_job_state.KEY) == values['import_raw']


@pytest.fixture(scope='module')
def feedback_archive(archive_documents):
    store, values = archive_documents
    for owner in feedback.publication.OWNERS.values():
        owner.publish(store.conn, revision=feedback.prepare(store.conn, owner))
    return archive_documents


def test_collection_feedback_archive_input_keeps_both_selected_originals(feedback_archive):
    store, values = feedback_archive
    for owner in feedback.publication.OWNERS.values():
        assert owner.selected(store.conn) == 0
        assert owner.value(store.conn, 0) == values['feedback_originals'][owner.source]


@pytest.fixture(scope='module')
def taste_archive(feedback_archive):
    store, values = feedback_archive
    for owner in taste.publication.OWNERS.values():
        owner.publish(store.conn, revision=taste.prepare(store.conn, owner))
    return feedback_archive


def test_taste_archive_input_keeps_all_three_selected_originals(taste_archive):
    store, values = taste_archive
    for owner in taste.publication.OWNERS.values():
        assert owner.selected(store.conn) == 0
        assert owner.value(store.conn, 0) == values['taste_originals'][owner.source]


@pytest.fixture(scope='module')
def document_archive(taste_archive):
    store, values = taste_archive
    values['import_revision'] = imports.prepare(store.conn)
    imports.publication.OWNER.publish(store.conn, revision=values['import_revision'])
    values['document_stages'] = {}
    for name, owner in documents.publication.OWNERS.items():
        identity = documents.stage(owner, store.conn)
        owner.publish(store.conn, stage=identity)
        values['document_stages'][name] = identity
    return taste_archive


def test_document_archive_input_keeps_import_recipe_and_canvas_selected(document_archive):
    store, values = document_archive
    assert imports.publication.OWNER.selected(store.conn) == values['import_revision']
    for name, owner in documents.publication.OWNERS.items():
        assert owner.selected(store.conn) == values['document_stages'][name]


@pytest.fixture(scope='module')
def archive_upgrade_inputs(upgraded_custody_inputs, document_archive):
    store, values = document_archive
    completed = capture_template(store, values)

    def retain_input(_store):
        return completed.result

    def get(target, **options):
        return upgraded_custody_inputs(93, target, 'collection-archive-upgrade', retain_input,
            prepared=completed, **options)
    get.release = lambda: upgraded_custody_inputs.release(93, 'collection-archive-upgrade')
    get.entity_programs = None
    return get


def assert_archives(template):
    conn, values = template.source.conn, template.result
    for module, key in ((feedback, 'feedback_originals'), (taste, 'taste_originals')):
        for owner in module.publication.OWNERS.values():
            assert owner.value(conn, 0) == values[key][owner.source]
    assert imports.publication.OWNER.value(conn, values['import_revision']) == values['import_raw']
    for name, owner in documents.publication.OWNERS.items():
        assert owner.value(conn, values['document_stages'][name], 'PLAYLIST')


def test_collection_archive_upgrade_input_keeps_all_exact_originals(archive_upgrade_inputs):
    template = archive_upgrade_inputs(93)
    assert_upgrade_phase(template, 93)
    assert_archives(template)


UPGRADE_CASES = [case for item in custody_upgrade_cases((93,))
                 for case in ([item, (93, 103, 'programs')] if item == (93, 103, 'converted') else [item])]


@pytest.mark.parametrize('version,target,phase', UPGRADE_CASES)
def test_collection_archive_upgrade_phase_keeps_history_and_exact_originals(archive_upgrade_inputs, version, target, phase):
    if target == 103 and phase == 'converted':
        restore, guards = Mock(), Mock()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(v103_entity_names, 'restore_programs', restore)
            patch.setattr(regular, 'after_owner_upgrade', guards)
            template = archive_upgrade_inputs(target, phase=phase)
        restore.assert_called_once()
        guards.assert_called_once_with(template.source.conn)
        archive_upgrade_inputs.entity_programs = restore.call_args
    elif phase == 'programs':
        template = archive_upgrade_inputs(target, phase='converted')
        conn = template.source.conn
        conn.execute('PRAGMA query_only=OFF')
        try:
            with conn:
                conn.execute('BEGIN')
                programs = archive_upgrade_inputs.entity_programs
                v103_entity_names.restore_programs(*programs.args, **programs.kwargs)
                regular.after_owner_upgrade(conn)
        finally:
            conn.execute('PRAGMA query_only=ON')
    else:
        template = archive_upgrade_inputs(target, phase=phase)
    assert_upgrade_phase(template, custody_predecessor(version, target) if phase == 'input' else target)
    assert_archives(template)


def test_collection_archive_serving_input_has_current_revision_guards(archive_upgrade_inputs):
    template = archive_upgrade_inputs(107)
    assert_upgrade_phase(template, 107)
    assert template.source._snapshot_revisions.conn is template.source.conn
    assert_archives(template)


def test_native_taste_and_feedback_archives_survive_real_orphan_collection(archive_upgrade_inputs, tmp_path):
    template = archive_upgrade_inputs(107)
    with template.open(tmp_path/'collected.db') as store, schema_inspection.reuse_tables(store.conn):
        regular.validate(store.conn)
        collection_recovery.collect_orphans(store.conn)
        values = template.result
        for owner in taste.publication.OWNERS.values():
            assert owner.selected(store.conn) is None
            assert owner.value(store.conn, 0) == values['taste_originals'][owner.source]
            assert json.loads(store.settings.get_setting(owner.source)) == values['taste_values'][owner.source]
        for owner in feedback.publication.OWNERS.values():
            assert owner.selected(store.conn) is None
            assert owner.value(store.conn, 0) == values['feedback_originals'][owner.source]
        assert feedback.account_auth_state.read(store.conn) == {1: 'Exact été label', 999: 'Historical account', 998: ''}
        assert len(feedback.album_feedback_storage.original(store.conn)) == 2
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_native_document_readers_keep_archives_and_reject_a_missing_custody_guard(archive_upgrade_inputs, tmp_path):
    template = archive_upgrade_inputs(107)
    with template.open(tmp_path/'documents.db') as store, schema_inspection.reuse_tables(store.conn):
        regular.validate(store.conn)
        values = template.result
        owner = imports.publication.OWNER
        assert owner.selected(store.conn) is None
        assert owner.value(store.conn, values['import_revision']) == values['import_raw']
        assert imports.spotify_job_state.load(store.conn) == imports.entity_names.account_document(values['import_value'])
        for name, owner in documents.publication.OWNERS.items():
            assert owner.selected(store.conn) is None
            assert owner.value(store.conn, values['document_stages'][name], 'PLAYLIST')
        assert store.playlist_recipes.get_recipe('PLAYLIST') == values['recipe']
        assert store.playlist_recipes.get_cluster_canvas('PLAYLIST') == values['canvas']
        assert store.playlist_recipes.get_recipe_created_ats() == {'PLAYLIST': 3}
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
        with store.conn:
            store.conn.execute('DROP TRIGGER '+documents.publication.OWNERS['recipes'].header+'_mapped_delete')
        with pytest.raises(MigrationError, match='mapped publication object'):
            regular.validate(store.conn)


def test_collection_archive_upgrade_closes_every_original_and_converted_input(archive_upgrade_inputs):
    sources = archive_upgrade_inputs.release()
    assert len(sources) == len(custody_upgrade_cases((93,))) // 2 + 1
    for source in sources:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            source.conn.execute('SELECT 1')
