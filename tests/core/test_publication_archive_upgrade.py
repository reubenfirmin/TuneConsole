"""Native cutovers retain each publication domain's exact original archive."""
import json
from contextlib import closing, contextmanager
from dataclasses import replace
import sqlite3

import pytest

from tests.core import test_clicked_publication as clicked
from tests.core import test_conflict_publication as conflicts
from tests.core import test_draft_publication as drafts
from tests.core import test_discovery_publication as discovery
from tests.core import test_metadata_publication as metadata
from tests.core import test_model_publication as models
from tests.core import test_pair_publication as pairs
from tests.core import test_tv_publication as tv
from tests.core import test_workflow_publication as workflows
from tests.core.test_genre_preferences_migration import bounded_vocabulary
from tests.store_fixture_support import install_numbered_input
from tests.archive_upgrade_support import completed_archive_input, archive_reader
from yt_playlist.core import genre_identity, group_evidence, group_storage, recommendation_custody, schema_definition
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.store import Store
from yt_playlist.library import playlist_gc_retry


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Archive SQL/file tests never start the web lifespan or sync worker."""


@pytest.fixture
def archive_input(request, tmp_path, bounded_vocabulary):
    """Exact empty predecessor input; each case owns its SQLite file and files.

    The numbered definition supplies historical DDL directly. The real runner
    admits it and executes the populated conversion under test, without
    replaying every earlier empty migration and unrelated regular change.
    """
    version = request.param
    store = Store(tmp_path / 'archive-input.db')
    try:
        with closing(sqlite3.connect(':memory:')) as source:
            source.execute('PRAGMA foreign_keys=ON')
            source.execute('BEGIN')
            for sql in schema_definition.statements(version=version):
                source.execute(sql)
            source.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',
                [(item.version, item.name) for item in registry.MIGRATIONS[:version]])
            source.executemany('INSERT INTO settings VALUES (?,?)',
                               schema_definition.SEED_SETTINGS.items())
            for sql in schema_definition.INITIAL_STATE:
                source.execute(sql)
            genre_identity.seed_catalog(source)
            source.commit()
            source.backup(store.conn)
        store.conn.execute('PRAGMA journal_mode=WAL')
        store._schema_compatibility = True
        store._bind_repositories()
        yield store
    finally:
        store.conn.close()


@contextmanager
def upgraded_archive(source, tmp_path, version):
    """Run one genuine registered owner cutover and reopen its native readers.

    Startup and pending regular changes are independent lower-layer owners;
    the registered runner validates this predecessor and converted structure.
    """
    upgraded = Store(tmp_path / 'archive-upgrade.db')
    try:
        upgraded.conn.execute('PRAGMA synchronous=OFF')
        source.conn.backup(upgraded.conn)
        upgraded.conn.execute('PRAGMA journal_mode=WAL')
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:version])
        upgraded.conn.close()
        upgraded = Store(tmp_path / 'archive-upgrade.db')
        upgraded._schema_compatibility = True
        upgraded._bind_repositories()
        yield upgraded
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        upgraded.conn.close()
@pytest.mark.parametrize('archive_input', [93, 94, 97], indirect=True)
def test_completed_archive_cutover_is_idempotent_and_admitted(archive_input):
    store = archive_input
    version = store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] + 1
    store.settings.set_setting('retained-archive-control', 'Exact été original')
    runner.migrate(store.conn, registry=registry.MIGRATIONS[:version])
    history = [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations')]
    store.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        before = reopened.conn.total_changes
        runner.migrate(reopened.conn, registry=registry.MIGRATIONS[:version])
        regular.validate(reopened.conn)
        assert reopened.conn.total_changes == before
        assert [tuple(row) for row in reopened.conn.execute('SELECT * FROM schema_migrations')] == history
        assert reopened.settings.get_setting('retained-archive-control') == 'Exact été original'
    finally:
        reopened.conn.close()


@pytest.fixture
def domain_archive_input(request, tmp_path, bounded_vocabulary):
    """Completed lower DDL; the populated registered domain cutover stays real."""
    store = Store(tmp_path / 'domain-archive.db')
    try:
        store.conn.execute('PRAGMA synchronous=OFF')
        with closing(sqlite3.connect(':memory:')) as schema:
            install_numbered_input(schema, request.param)
            schema.backup(store.conn)
        store.conn.execute('PRAGMA journal_mode=WAL')
        store._schema_compatibility = request.param < 103
        store._rebuild_source_version = None
        store._schema_recovery_pending = False
        store._bind_repositories()
        yield store
    finally:
        store.conn.close()


@pytest.mark.parametrize('domain_archive_input', [79], indirect=True)
def test_metadata_archive_cutover_keeps_attempts_and_continuing_choices(domain_archive_input, tmp_path):
    store, conn = domain_archive_input, domain_archive_input.conn
    entry, _, key = metadata.seed(store)
    metadata.preparation.JOB.install(conn)
    metadata.finish(conn)
    identity = metadata.publish(conn)
    assert metadata.publication.selected(conn) == identity
    with upgraded_archive(store, tmp_path, 80) as upgraded:
        assert metadata.publication.selected(upgraded.conn) is None
        metadata.choose_field(upgraded, entry, 'bpm', 140)
        assert metadata.metadata_provenance(upgraded, entry)['fields']['bpm']['value'] == 140
        assert upgraded.enrichment_state.enrichment_attempts(key)['a']['fields']['bpm'] == 120


@pytest.mark.parametrize('domain_archive_input', [79], indirect=True)
def test_conflict_archive_cutover_keeps_exact_heads_and_continuing_choices(domain_archive_input, tmp_path):
    store, conn = domain_archive_input, domain_archive_input.conn
    conflict, original = conflicts.seed(store, [{'provider': 'a', 'value': 120}], resolved=0,
                                        video_id='conflict-video', title='Conflict song')
    conflicts.preparation.JOB.install(conn)
    conflicts.finish(conn)
    identity = conflicts.publish(conn)
    heads = list(map(tuple, conn.execute('SELECT * FROM ' + conflicts.preparation.HEAD)))
    assert conflicts.publication.OWNER.selected(conn) == identity
    with upgraded_archive(store, tmp_path, 80) as upgraded:
        assert conflicts.publication.OWNER.selected(upgraded.conn) is None
        assert list(map(tuple, upgraded.conn.execute('SELECT * FROM ' + conflicts.preparation.HEAD))) == heads
        assert upgraded.conn.execute('SELECT count(*) FROM ' + conflicts.preparation.HEAD +
                                     ' WHERE conflict_id=?', (conflict,)).fetchone()[0] == 1
        conflicts.choose_field(upgraded, original['track_id'], 'bpm', 140)
        assert conflicts.metadata_provenance(upgraded, original['track_id'])['fields']['bpm']['value'] == 140


@pytest.mark.parametrize('domain_archive_input', [93], indirect=True)
def test_model_archive_cutover_keeps_exact_encoders_and_tokens(domain_archive_input, tmp_path):
    store, conn = domain_archive_input, domain_archive_input.conn
    sources = models.seed(store)
    revisions = models.prepare(store)
    models.publication.publish(conn, **revisions)
    assert models.publication.selected(conn) == (revisions['artifact_revision'], revisions['fingerprint_revision'])
    with upgraded_archive(store, tmp_path, 94) as upgraded:
        assert models.publication.selected(upgraded.conn) is None
        assert {name: models.model_state.get(upgraded.conn, name) for name in sources} == sources


def assert_early_archive_consumers(store, version, expected):
    conn = store.conn
    if version >= 80:
        assert metadata.publication.selected(conn) is None
        assert conflicts.publication.OWNER.selected(conn) is None
    if version >= 94:
        assert models.publication.selected(conn) is None
        assert {key: models.model_state.get(conn, key) for key in expected['model_sources']} == expected['model_sources']
    if version >= 101:
        assert discovery.publication.OWNERS[expected['discovery_owner']].selected(conn) is None
        assert store.discovery.get_discovered_artists() == expected['artists']
    assert store.enrichment_state.enrichment_attempts(expected['key'])['a']['fields']['bpm'] == 120
    assert conn.execute('SELECT count(*) FROM ' + conflicts.preparation.HEAD +
                        ' WHERE conflict_id=?', (expected['conflict'],)).fetchone()[0] == 1
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


def retained_early_archive_rows(conn, expected):
    owner = discovery.publication.OWNERS[expected['discovery_owner']]
    tables = [conflicts.preparation.HEAD, *(owner.table('input_' + kind) for kind in owner.kinds)]
    return {table: list(map(tuple, conn.execute('SELECT * FROM ' + table))) for table in tables}


@pytest.mark.parametrize('completed_archive_input', [
    (79, 80), (80, 92), (92, 94), (94, 98), (98, 100), (100, 102), (103, 104), (104, 107)
], indirect=True, ids=['metadata', 'genre-contract', 'models', 'documents', 'listening',
                       'discovery', 'domain-contracts', 'native-admission'])
def test_registered_upgrade_keeps_early_archives_and_native_precedence(completed_archive_input):
    conn, path, source, target, expected = completed_archive_input
    retained = retained_early_archive_rows(conn, expected)
    if source == 79:
        assert metadata.publication.selected(conn) is not None
        assert conflicts.publication.OWNER.selected(conn) is not None
        assert models.publication.selected(conn) is not None
        assert discovery.publication.OWNERS[expected['discovery_owner']].selected(conn) is not None
        with archive_reader(path, source) as reader:
            assert reader.discovery.get_discovered_artists() == expected['artists']
    history = list(map(tuple, conn.execute('SELECT version,name FROM schema_migrations ORDER BY version')))
    runner.migrate(conn, registry=registry.MIGRATIONS[:target])
    assert list(map(tuple, conn.execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        *history, *[(item.version, item.name) for item in registry.MIGRATIONS[source:target]]]
    assert retained_early_archive_rows(conn, expected) == retained
    with archive_reader(path, target) as reader:
        assert_early_archive_consumers(reader, target, expected)
    regular.validate(conn)


@pytest.mark.parametrize('completed_archive_input', [(103, 103)], indirect=True, ids=['renamed-input'])
def test_name_cutover_restores_early_archive_programs_and_custody(completed_archive_input):
    from yt_playlist.core.migrations import v103_entity_names

    conn, path, _, target, expected = completed_archive_input
    retained = retained_early_archive_rows(conn, expected)
    programs = list(conn.execute("SELECT type,name,sql FROM sqlite_schema WHERE type IN ('view','trigger') ORDER BY rowid"))
    with conn:
        conn.execute('BEGIN')
        for kind, name, _sql in reversed(programs):
            conn.execute('DROP ' + kind + ' "' + name.replace('"', '""') + '"')
        v103_entity_names.restore_programs(conn, programs, retention_progress=False)
    runner.validate_version(conn, 103, foreign_keys=False, data=False)
    with archive_reader(path, target) as reader:
        assert_early_archive_consumers(reader, target, expected)
    assert retained_early_archive_rows(conn, expected) == retained
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with conn:
            conn.execute('DELETE FROM ' + conflicts.preparation.CANDIDATES)


@pytest.mark.parametrize('completed_archive_input', [(107, 107)], indirect=True, ids=['native-input'])
def test_native_archive_reopen_admits_history_without_losing_originals(completed_archive_input):
    conn, path, _, _, expected = completed_archive_input
    retained = retained_early_archive_rows(conn, expected)
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    reopened = Store(path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert retained_early_archive_rows(reopened.conn, expected) == retained
        assert list(map(tuple, reopened.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('completed_archive_input', [(107, 107)], indirect=True, ids=['native-input'])
def test_native_archive_sql_admission_is_idempotent(completed_archive_input):
    conn, _, _, _, expected = completed_archive_input
    retained = retained_early_archive_rows(conn, expected)
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    regular.validate(conn)
    before = conn.total_changes
    regular.migrate(conn)
    assert conn.total_changes == before
    assert retained_early_archive_rows(conn, expected) == retained
    assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history


@pytest.mark.parametrize('completed_archive_input', [(107, 107)], indirect=True, ids=['native-input'])
def test_native_archive_consumers_keep_exact_models_and_discovery(completed_archive_input):
    _, path, _, _, expected = completed_archive_input
    with archive_reader(path, 107) as reader:
        assert_early_archive_consumers(reader, 107, expected)


@pytest.mark.parametrize('completed_archive_input', [(107, 107)], indirect=True, ids=['native-input'])
@pytest.mark.parametrize('field_owner', ['metadata', 'conflict'])
def test_native_archive_continuing_choices_keep_separate_recordings(completed_archive_input, field_owner):
    conn, path, _, _, expected = completed_archive_input
    assert expected['entry'] != expected['conflict_entry']
    entry = expected['entry'] if field_owner == 'metadata' else expected['conflict_entry']
    with archive_reader(path, 107) as reader:
        metadata.choose_field(reader, entry, 'bpm', 140)
        assert metadata.metadata_provenance(reader, entry)['fields']['bpm']['value'] == 140
        other = expected['conflict_entry'] if field_owner == 'metadata' else expected['entry']
        assert metadata.metadata_provenance(reader, other)['fields']['bpm']['value'] != 140
        assert reader.enrichment_state.enrichment_attempts(expected['key'])['a']['fields']['bpm'] == 120
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('archive_input', [93], indirect=True)
def test_draft_archive_survives_registered_native_cutover(archive_input, tmp_path):
    store = archive_input
    recipe, raw = drafts.seed(store)
    owner = drafts.publication.OWNER
    stage = drafts.stage(store.conn)
    owner.publish(store.conn, stage=stage)
    assert owner.selected(store.conn) == stage

    with upgraded_archive(store, tmp_path, 94) as upgraded:
        assert owner.selected(upgraded.conn) is None
        assert owner.value(upgraded.conn, stage, recipe) == raw
        assert upgraded.road_trip.get_road_trip_draft(recipe) == json.loads(raw)


@pytest.mark.parametrize('archive_input', [94], indirect=True)
@pytest.mark.parametrize('surface', clicked.library_pool_state.SURFACES)
def test_clicked_archive_survives_registered_native_cutover(archive_input, tmp_path, surface):
    store = archive_input
    originals, _ = clicked.seed(store)
    # Each lane independently owns its exact original and native parameters.
    with store.conn:
        store.conn.execute('DELETE FROM rec_proposals WHERE surface<>?', (surface,))
    owner = clicked.publication.OWNER
    stage = clicked.ready(store.conn)
    owner.publish(store.conn, stage=stage)
    assert owner.selected(store.conn) == stage

    with upgraded_archive(store, tmp_path, 95) as upgraded:
        assert clicked.publication.native(upgraded.conn)
        assert owner.selected(upgraded.conn) is None
        assert owner.value(upgraded.conn, stage, surface) == originals[surface]
        assert upgraded.rec_proposals.get_proposals(surface)['parameters'] == (
            json.loads(originals[surface])['parameters'])


@pytest.mark.parametrize('archive_input', [94], indirect=True)
@pytest.mark.parametrize('arm', ['incumbent', 'challenger'])
@pytest.mark.parametrize('format', ['mixes', 'albums', 'radio'])
def test_pair_archive_survives_registered_native_cutover(archive_input, tmp_path, arm, format):
    store = archive_input
    originals, run_id = clicked.seed(store)
    surface = f'model_pair:{run_id}:{arm}:{format}'
    with store.conn:
        store.conn.execute('DELETE FROM rec_proposals WHERE surface<>?', (surface,))
    owner = pairs.publication.OWNER
    stage = pairs.ready(store.conn)
    owner.publish(store.conn, stage=stage)
    assert owner.selected(store.conn) == stage

    with upgraded_archive(store, tmp_path, 95) as upgraded:
        assert owner.selected(upgraded.conn) is None
        assert owner.value(upgraded.conn, stage, surface) == originals[surface]
        assert upgraded.rec_proposals.get_proposals(surface) is not None
        assert owner.root_stage(upgraded.conn, stage)['complete']


@pytest.mark.parametrize('archive_input', [94], indirect=True)
def test_pair_archive_keeps_known_and_unresolved_custody_after_cutover(archive_input, tmp_path):
    from tests.repos.test_typed_recommendation_publication import plan
    from yt_playlist.controllers.recommendation_import import import_plan

    store = archive_input
    store._snapshot_revisions = Revisions(store.conn)
    # Completed lower-layer output: no discovery, profiling or candidate ranking
    # is needed to establish real FK-backed generation and version custody.
    prepared = {'generation': 'archive-groups', 'policy': '{}', 'fingerprint': 'archive-input',
        'at': 1000., 'serial': 0, 'payload': '{}', 'lineage': [], 'unknown': [], 'edges': [],
        'groups': [{'group_id': 'archive-group', 'version': 'archive-version', 'parent_version': None,
                    'payload': '{"leaf":true,"membership_known":true,"size":0}', 'members': []}]}
    # Native atomic import is sufficient fixture construction; the leased
    # producer's resumable orchestration has its own publication owners.
    with store.conn.atomic():
        manifest = group_storage.install_manifest(store.conn, {}, {}.get)
        group_id = group_storage.install_header(store.conn, prepared, {}, {}, manifest)
        group_storage.install_versions(store.conn, prepared, group_id, {}.get)
        store.conn.execute('INSERT INTO group_publications VALUES (?,?)', (group_id, 1000.))
        generation = import_plan(store.conn, replace(plan(store, candidates=[]), slots=()),
                                 1000.)
    surface = 'model_pair:1:incumbent:mixes'
    known_key = 'proposal:' + surface + ':context'
    unknown_key = 'proposal:' + surface + ':choice_context'
    with store.conn.atomic():
        store.conn.execute('INSERT INTO rec_proposals VALUES (?,?,NULL)',
            (surface, '{"run_id":1,"data":{"items":[],"context":{},"caption":"東京"}}'))
        group_evidence.retain(store.conn, 'comparison', known_key,
            {'group_generation': 'archive-groups', 'musical_groups': [
                {'version': 'archive-version', 'group_id': 'archive-group'}]}, 1000)
        recommendation_custody.retain(store.conn, 'comparison', known_key, generation)
        group_evidence.retain_unknown_context(store.conn, 'comparison', unknown_key,
                                              'original:unavailable', 'missing_context', 1000)
    owner = pairs.publication.OWNER
    stage = pairs.ready(store.conn)
    owner.publish(store.conn, stage=stage)
    assert owner.selected(store.conn) == stage
    expected = [tuple(row) for row in store.conn.execute(
        'SELECT * FROM ' + owner.table('root_map') + ' WHERE stage=? ORDER BY owner,source_key', (stage,))]
    assert len(expected) >= 5

    with upgraded_archive(store, tmp_path, 95) as upgraded:
        assert owner.selected(upgraded.conn) is None
        assert owner.root_stage(upgraded.conn, stage)['complete']
        assert [tuple(row) for row in upgraded.conn.execute(
            'SELECT * FROM ' + owner.table('root_map') + ' WHERE stage=? ORDER BY owner,source_key', (stage,))] == expected
        assert not upgraded.conn.execute('SELECT 1 FROM group_evidence_owners WHERE external_key IN (?,?)',
                                         (known_key, unknown_key)).fetchone()
        pin = owner.pin_key(stage, known_key)
        assert upgraded.conn.execute('SELECT generation_id FROM group_evidence_generations '
            'WHERE kind=\'comparison\' AND external_key=?', (pin,)).fetchone()
        assert upgraded.conn.execute('SELECT external_version FROM group_evidence_versions '
            'WHERE kind=\'comparison\' AND external_key=?', (pin,)).fetchone()[0] == 'archive-version'
        assert upgraded.conn.execute('SELECT generation_id FROM recommendation_generation_refs '
            'WHERE kind=\'comparison\' AND external_key=?', (pin,)).fetchone()[0] == generation
        assert upgraded.conn.execute('SELECT source_context FROM unresolved_group_contexts '
            'WHERE kind=\'comparison\' AND external_key=?', (owner.pin_key(stage, unknown_key),)).fetchone()[0] == 'original:unavailable'


@pytest.mark.parametrize('archive_input', [94], indirect=True)
@pytest.mark.parametrize('name', tuple(tv.publication.OWNERS))
def test_tv_archive_survives_registered_native_cutover(archive_input, tmp_path, name):
    store = archive_input
    values, originals = tv.seed(store)
    owner = tv.publication.OWNERS[name]
    with store.conn:
        store.conn.execute('DELETE FROM rec_proposals WHERE surface<>?', (owner.source,))
    tv.aliases(store.conn)
    revision = tv.prepare(owner, store.conn)
    owner.publish(store.conn, revision=revision)
    assert owner.selected(store.conn) == revision

    with upgraded_archive(store, tmp_path, 95) as upgraded:
        assert owner.selected(upgraded.conn) is None
        assert owner.value(upgraded.conn, revision) == originals[owner.source]
        assert upgraded.rec_proposals.get_proposals(owner.source) == values[owner.source]


@pytest.mark.parametrize('archive_input', [97], indirect=True)
@pytest.mark.parametrize('name', tuple(workflows.publication.OWNERS))
def test_workflow_archive_survives_registered_native_cutover(archive_input, tmp_path, name):
    store = archive_input
    _, originals, account, _ = workflows.seed(store)
    owner = workflows.publication.OWNERS[name]
    stage = workflows.stage(owner, store.conn)
    owner.publish(store.conn, stage=stage)
    assert owner.selected(store.conn) == stage

    with upgraded_archive(store, tmp_path, 98) as upgraded:
        assert owner.selected(upgraded.conn) is None
        for key, raw in originals.items():
            if workflows.publication.owner_for(key) is owner:
                assert owner.value(upgraded.conn, stage, key) == raw
        if name == 'playlists':
            receipts = upgraded.settings.spotify_transfer_receipts(account)
            assert len(receipts['fingerprint:東京']['source_video_ids']) == 3
        elif name == 'gc':
            assert len(playlist_gc_retry.states(upgraded)) == 1


@pytest.mark.parametrize('archive_input', [97], indirect=True)
def test_native_workflow_archive_keeps_mapped_object_validation(archive_input, tmp_path):
    store = archive_input
    workflows.seed(store)
    owner = workflows.publication.OWNERS['gc']
    stage = workflows.stage(owner, store.conn)
    owner.publish(store.conn, stage=stage)
    with upgraded_archive(store, tmp_path, 98) as upgraded:
        definition = upgraded.conn.execute('SELECT sql FROM sqlite_schema WHERE name=?',
                                          (owner.header + '_mapped_delete',)).fetchone()[0]
        with upgraded.conn:
            upgraded.conn.execute('DROP TRIGGER ' + owner.header + '_mapped_delete')
        with pytest.raises(MigrationError, match='mapped publication object'):
            regular.validate(upgraded.conn)
        # Restore the actual definition before the final integrity check.
        with upgraded.conn:
            upgraded.conn.execute(definition)
