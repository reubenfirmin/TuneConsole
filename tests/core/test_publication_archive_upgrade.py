"""Native cutovers retain each publication domain's exact original archive."""
import json
from contextlib import closing, contextmanager
from dataclasses import replace
import sqlite3

import pytest

from tests.core import test_clicked_publication as clicked
from tests.core import test_collection_document_publication as documents
from tests.core import test_collection_feedback_publication as feedback
from tests.core import test_conflict_publication as conflicts
from tests.core import test_draft_publication as drafts
from tests.core import test_discovery_checkpoint_publication as checkpoints
from tests.core import test_discovery_publication as discovery
from tests.core import test_import_publication as imports
from tests.core import test_library_sync_publication as sync
from tests.core import test_metadata_publication as metadata
from tests.core import test_model_publication as models
from tests.core import test_pair_publication as pairs
from tests.core import test_playback_publication as playback
from tests.core import test_radio_origin_publication as origins
from tests.core import test_trial_publication as trials
from tests.core import test_taste_control_publication as taste
from tests.core import test_tv_publication as tv
from tests.core import test_workflow_publication as workflows
from tests.core.test_predecessor_writes import installed
from tests.core.test_metadata_owner_cutover import predecessor
from tests.core.test_genre_preferences_migration import bounded_vocabulary
from yt_playlist.core import collection_recovery, genre_identity, group_evidence, group_storage, recommendation_custody, schema_definition
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


def test_v79_upgrade_preserves_metadata_conflicts_and_discovery(predecessor, tmp_path):
    store, conn = predecessor, predecessor.conn
    # Discovery establishes the shared Song/Artist recording before metadata
    # captures its attempt; later catalog seeding would reset that attempt.
    discovery.seed(store)
    entry, recording, key = metadata.seed(store)
    # Separate recordings preserve both original continuing-writer checks.
    conflict, conflict_original = conflicts.seed(
        store, [{'provider': 'a', 'value': 120}], resolved=0,
        video_id='conflict-video', title='Conflict song')
    assert conflict_original['track_id'] != entry
    assert conflict_original['recording_id'] != recording
    assert store.enrichment_state.enrichment_attempts(key)['a']['fields']['bpm'] == 120
    encoder_sources = models.seed(store)

    metadata.preparation.JOB.install(conn)
    metadata.finish(conn)
    metadata_selection = metadata.publish(conn)
    conflicts.preparation.JOB.install(conn)
    conflicts.finish(conn)
    conflict_selection = conflicts.publish(conn)
    discovery_owner, discovery_selection = discovery.ready(conn)
    discovery_owner.publish(conn, stage=discovery_selection)
    encoder_revisions = models.prepare(store)
    models.publication.publish(conn, **encoder_revisions)
    assert metadata.publication.selected(conn) == metadata_selection
    assert conflicts.publication.OWNER.selected(conn) == conflict_selection
    assert discovery_owner.selected(conn) == discovery_selection
    assert models.publication.selected(conn) == (
        encoder_revisions['artifact_revision'], encoder_revisions['fingerprint_revision'])
    expected_artists = store.discovery.get_discovered_artists()
    retained = {kind: list(map(tuple, conn.execute(
        'SELECT * FROM ' + discovery_owner.table('input_' + kind))))
        for kind in discovery_owner.kinds}
    conflict_heads = list(map(tuple, conn.execute(
        'SELECT * FROM ' + conflicts.preparation.HEAD + ' ORDER BY conflict_id,generation')))

    upgraded = Store(tmp_path / 'native-v79-archives.db')
    try:
        conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS)
        upgraded._bind_repositories()
        assert metadata.publication.selected(upgraded.conn) is None
        assert conflicts.publication.OWNER.selected(upgraded.conn) is None
        assert models.publication.selected(upgraded.conn) is None
        assert all(models.model_state.get(upgraded.conn, name) == raw
                   for name, raw in encoder_sources.items())
        regular.validate(upgraded.conn)
        regular.migrate(upgraded.conn)
        upgraded.init_schema(defer_metadata=True)
        assert discovery_owner.selected(upgraded.conn) is None
        assert upgraded.discovery.get_discovered_artists() == expected_artists
        for kind, rows in retained.items():
            assert list(map(tuple, upgraded.conn.execute(
                'SELECT * FROM ' + discovery_owner.table('input_' + kind)))) == rows
        assert list(map(tuple, upgraded.conn.execute(
            'SELECT * FROM ' + conflicts.preparation.HEAD + ' ORDER BY conflict_id,generation'))) == conflict_heads
        assert upgraded.conn.execute('SELECT count(*) FROM ' + conflicts.preparation.HEAD +
            ' WHERE conflict_id=?', (conflict,)).fetchone()[0] == 1
        metadata.choose_field(upgraded, entry, 'bpm', 140)
        assert metadata.metadata_provenance(upgraded, entry)['fields']['bpm']['value'] == 140
        assert upgraded.enrichment_state.enrichment_attempts(key)['a']['fields']['bpm'] == 120
        conflicts.choose_field(upgraded, conflict_original['track_id'], 'bpm', 140)
        assert conflicts.metadata_provenance(
            upgraded, conflict_original['track_id'])['fields']['bpm']['value'] == 140
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        upgraded.conn.close()


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


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_taste_and_feedback_upgrade_retains_originals_after_collection(installed, tmp_path):
    store, _ = installed
    feedback_originals = feedback.seed(store)
    taste_values, taste_originals = taste.seed(store)
    for module in (feedback, taste):
        for owner in module.publication.OWNERS.values():
            revision = module.prepare(store.conn, owner)
            owner.publish(store.conn, revision=revision)
            assert owner.selected(store.conn) == revision

    upgraded = Store(tmp_path / 'taste-feedback-archive-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS)
        upgraded.init_schema(defer_metadata=True)
        regular.validate(upgraded.conn)
        collection_recovery.collect_orphans(upgraded.conn)
        for owner in taste.publication.OWNERS.values():
            assert owner.selected(upgraded.conn) is None
            assert owner.value(upgraded.conn, 0) == taste_originals[owner.source]
            assert json.loads(upgraded.settings.get_setting(owner.source)) == taste_values[owner.source]
        for owner in feedback.publication.OWNERS.values():
            assert owner.selected(upgraded.conn) is None
            assert owner.value(upgraded.conn, 0) == feedback_originals[owner.source]
        assert feedback.account_auth_state.read(upgraded.conn) == {
            1: 'Exact été label', 999: 'Historical account', 998: ''}
        # The later alias judgment still wins in the native owner.
        assert len(feedback.album_feedback_storage.original(upgraded.conn)) == 2
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_document_upgrade_retains_import_recipe_and_canvas_archives(installed, tmp_path):
    store, _ = installed
    # Both former upgrade owners used this exact source set. Seed it once and
    # publish each domain before the shared frozen migration chain.
    import_value, import_raw, recipe, canvas = documents.seed(store)
    import_owner = imports.publication.OWNER
    import_revision = imports.prepare(store.conn)
    import_owner.publish(store.conn, revision=import_revision)
    identities = {}
    for name, owner in documents.publication.OWNERS.items():
        identities[name] = documents.stage(owner, store.conn)
        owner.publish(store.conn, stage=identities[name])
    assert import_owner.selected(store.conn) == import_revision
    for name, owner in documents.publication.OWNERS.items():
        assert owner.selected(store.conn) == identities[name]

    upgraded = Store(tmp_path / 'document-archive-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS)
        upgraded.init_schema(defer_metadata=True)
        regular.validate(upgraded.conn)
        assert import_owner.selected(upgraded.conn) is None
        assert import_owner.value(upgraded.conn, import_revision) == import_raw
        assert imports.spotify_job_state.load(upgraded.conn) == imports.entity_names.account_document(import_value)
        for name, owner in documents.publication.OWNERS.items():
            assert owner.selected(upgraded.conn) is None
            assert owner.value(upgraded.conn, identities[name], 'PLAYLIST')
        assert upgraded.playlist_recipes.get_recipe('PLAYLIST') == recipe
        assert upgraded.playlist_recipes.get_cluster_canvas('PLAYLIST') == canvas
        assert upgraded.playlist_recipes.get_recipe_created_ats() == {'PLAYLIST': 3}
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()
        with upgraded.conn:
            upgraded.conn.execute('DROP TRIGGER ' + documents.publication.OWNERS['recipes'].header + '_mapped_delete')
        with pytest.raises(MigrationError, match='mapped publication object'):
            regular.validate(upgraded.conn)
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_context_upgrade_preserves_sync_playback_origins_checkpoints_and_trials(installed, tmp_path):
    store, _ = installed
    # These fixtures share the same First/Second account identities and Song
    # recording. Finish every continuing write before selecting any archive.
    sync_value = json.loads(sync.seed(store))
    del sync_value['accounts']['999']
    del sync_value['accounts']['1']['resources']['playlists']['items']['missing-playlist']
    sync_raw = json.dumps(sync_value)
    store.settings.set_setting(sync.sync_state.KEY, sync_raw)
    checkpoints.seed(store)
    playback_originals = playback.seed(store)
    # The native converter requires admitted bindings; the installed reader's
    # separate unknown-binding controls retain their broader compatibility.
    bindings = playback.preparation.BINDINGS_KEY
    playback_originals[bindings] = json.dumps(json.loads(playback_originals[bindings])[:2])
    store.settings.set_setting(bindings, playback_originals[bindings])
    origin_originals = origins.seed(store)
    trial_values, trial_originals = trials.seed(store)
    # Frozen checkpoint conversion requires existing references and the exact
    # canonical artist spelling. Separate compatibility owners cover the rest.
    with store.conn:
        for name, suffix in (('fetch-history', 'unknown-release'), ('artist-images', '01'),
                             ('artist-images', '999'), ('provider-refresh', 'unknown')):
            store.conn.execute('DELETE FROM settings WHERE key=?',
                               (checkpoints.preparation.PREFIXES[name] + suffix,))

    for owner in sync.publication.OWNERS.values():
        owner.publish(store.conn, revision=sync.prepare(store.conn, owner))
        assert owner.selected(store.conn) == 0
    for owner in playback.publication.OWNERS.values():
        owner.publish(store.conn, revision=playback.prepare(store.conn, owner))
        assert owner.selected(store.conn) == 0
    expected_playback = playback.album_playback_context.load(store.conn)
    origins.publication.publish(store.conn, revision=origins.prepare(store.conn))
    assert origins.publication.selected(store.conn) == 0
    origin_heads = {source: origins.head(store.conn, source) for source in origin_originals}
    trial_revisions = {}
    for name, owner in trials.publication.OWNERS.items():
        trial_revisions[name] = trials.ready(owner, store.conn)
        owner.publish(store.conn, revision=trial_revisions[name])
        assert owner.selected(store.conn) == trial_revisions[name]
    with checkpoints.connect(store) as job:
        checkpoint_rows = {}
        for name, owner in checkpoints.publication.OWNERS.items():
            identity = checkpoints.ready(job, name)
            owner.publish(job, stage=identity)
            assert owner.selected(job) == identity
            checkpoint_rows[name] = [tuple(row) for row in job.execute(
                'SELECT * FROM ' + owner.table('rows') + ' ORDER BY source')]

    for owner in (*sync.publication.OWNERS.values(), *playback.publication.OWNERS.values()):
        assert owner.selected(store.conn) == 0
    assert origins.publication.selected(store.conn) == 0
    for name, owner in trials.publication.OWNERS.items():
        assert owner.selected(store.conn) == trial_revisions[name]

    upgraded = Store(tmp_path / 'context-archive-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS)
        upgraded.init_schema(defer_metadata=True)
        regular.validate(upgraded.conn)
        assert sync.publication.RESULT.selected(upgraded.conn) is None
        assert sync.publication.STAMPS.selected(upgraded.conn) is None
        assert sync.sync_state.read(upgraded.conn) == sync_value
        assert sync.sync_state.stamp(upgraded.conn, 'last_sync_at') == '10.5'
        assert sync.publication.RESULT.value(upgraded.conn, 0) == sync_raw
        assert sync.publication.STAMPS.value(upgraded.conn, 'last_sync_at', 0) == ' 10.50 '
        for owner in playback.publication.OWNERS.values():
            assert owner.selected(upgraded.conn) is None
            assert owner.value(upgraded.conn, 0) == playback_originals[owner.source]
        assert playback.album_playback_context.load(upgraded.conn) == expected_playback
        assert playback.playback_bindings.find(
            upgraded.conn, 1, 'LM', 'video', 13, 100)['opportunity_id'] == 'later-menu'
        assert origins.publication.selected(upgraded.conn) is None
        assert origins.radio_provenance.ids(upgraded.conn) == ['LM', 'missing', 'shared']
        for source, raw in origin_originals.items():
            assert origins.preparation.read_original(upgraded.conn, origin_heads[source]) == raw
        for name, owner in checkpoints.publication.OWNERS.items():
            assert owner.selected(upgraded.conn) is None
            assert [tuple(row) for row in upgraded.conn.execute(
                'SELECT * FROM ' + owner.table('rows') + ' ORDER BY source')] == checkpoint_rows[name]
        assert checkpoints.provider_frontiers.read(upgraded.conn, 'known')['fingerprint'] == 'exact été'
        assert checkpoints.discovery_checkpoints.fetched(upgraded.conn, 'release')
        for name, owner in trials.publication.OWNERS.items():
            assert owner.selected(upgraded.conn) is None
            assert owner.value(upgraded.conn, trial_revisions[name]) == trial_originals[owner.source]
            assert upgraded.rec_proposals.get_proposals(owner.source) == trial_values[owner.source]
        regular.migrate(upgraded.conn)
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        upgraded.conn.close()
