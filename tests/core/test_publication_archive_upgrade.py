"""One populated frozen upgrade owns compatible publication archive domains."""
import json

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
from yt_playlist.core import collection_recovery
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.store import Store
from yt_playlist.library import playlist_gc_retry


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


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_populated_upgrade_preserves_archives_and_native_precedence(installed, tmp_path):
    store, _ = installed
    # Capture all continuing inputs before preparation: later catalog changes
    # would legitimately invalidate another owner's previously selected proof.
    tv_values, tv_originals = tv.seed(store)
    # Establish the shared Song/Artist recording before creating the draft;
    # otherwise a later alias merge legitimately rewrites that saved draft.
    recipe, draft_raw = drafts.seed(store)
    ranked_originals, run_id = clicked.seed(store)
    # The draft fixture already owns the one master identity. Workflow receipt
    # attribution uses its own explicit account, independently of that flag.
    _, workflow_originals, account, _ = workflows.seed(store, master=False)
    # Pair and clicked publication consume the same original ranked fixture.
    # Capture real comparison roots after every catalog input is established.
    pairs.roots(store, run_id)

    draft_owner, clicked_owner = drafts.publication.OWNER, clicked.publication.OWNER
    pair_owner = pairs.publication.OWNER
    draft_stage = drafts.stage(store.conn)
    draft_owner.publish(store.conn, stage=draft_stage)
    clicked_stage = clicked.ready(store.conn)
    clicked_owner.publish(store.conn, stage=clicked_stage)
    pair_stage = pairs.ready(store.conn)
    pair_owner.publish(store.conn, stage=pair_stage)
    tv.publish_all(store)
    workflow_stages = {}
    for name, owner in workflows.publication.OWNERS.items():
        workflow_stages[name] = workflows.stage(owner, store.conn)
        owner.publish(store.conn, stage=workflow_stages[name])

    # Each archive must genuinely be selected before the single native upgrade.
    assert draft_owner.selected(store.conn) == draft_stage
    assert clicked_owner.selected(store.conn) == clicked_stage
    assert pair_owner.selected(store.conn) == pair_stage
    for owner in tv.publication.OWNERS.values():
        assert owner.selected(store.conn) == 0
    for name, owner in workflows.publication.OWNERS.items():
        assert owner.selected(store.conn) == workflow_stages[name]

    upgraded = Store(tmp_path / 'publication-archive-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS)
        upgraded.init_schema(defer_metadata=True)
        regular.validate(upgraded.conn)

        assert draft_owner.selected(upgraded.conn) is None
        assert draft_owner.value(upgraded.conn, draft_stage, recipe) == draft_raw
        assert upgraded.road_trip.get_road_trip_draft(recipe) == json.loads(draft_raw)

        assert clicked.publication.native(upgraded.conn)
        assert clicked_owner.selected(upgraded.conn) is None
        for surface in clicked.library_pool_state.SURFACES:
            assert clicked_owner.value(upgraded.conn, clicked_stage, surface) == ranked_originals[surface]
            assert (upgraded.rec_proposals.get_proposals(surface)['parameters']
                    == json.loads(ranked_originals[surface])['parameters'])

        assert pair_owner.selected(upgraded.conn) is None
        for surface, raw in ranked_originals.items():
            if surface.startswith('model_pair:'):
                assert pair_owner.value(upgraded.conn, pair_stage, surface) == raw
                assert upgraded.rec_proposals.get_proposals(surface) is not None
        assert pair_owner.root_stage(upgraded.conn, pair_stage)['complete']

        for surface, owner in tv.publication.SOURCES.items():
            assert owner.selected(upgraded.conn) is None
            assert owner.value(upgraded.conn, 0) == tv_originals[surface]
            assert upgraded.rec_proposals.get_proposals(surface) == tv_values[surface]

        for key, raw in workflow_originals.items():
            owner = workflows.publication.owner_for(key)
            assert owner.selected(upgraded.conn) is None
            assert owner.value(upgraded.conn, workflow_stages[owner.name], key) == raw
        receipts = upgraded.settings.spotify_transfer_receipts(account)
        assert len(receipts['fingerprint:東京']['source_video_ids']) == 3
        assert len(playlist_gc_retry.states(upgraded)) == 1
        regular.migrate(upgraded.conn)
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()

        with upgraded.conn:
            upgraded.conn.execute('DROP TRIGGER ' + workflows.publication.OWNERS['gc'].header + '_mapped_delete')
        with pytest.raises(MigrationError, match='mapped publication object'):
            regular.validate(upgraded.conn)
    finally:
        upgraded.conn.close()


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
