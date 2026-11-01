"""Recovered installed readers and incomplete plans own independent exact files."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import policy_recovery, preparation_files as recovery
from yt_playlist.core.preparation import document_plan
from yt_playlist.core.store import Store


def selected_owner(store, kind):
    if kind == 'home':
        from tests.core.test_home_output_preparation import seed
        from tests.core.test_home_publication import ready
        from yt_playlist.core.preparation import home_outputs as preparation, home_publication as publication
        expected = seed(store)
        with store.conn:
            store.conn.execute('DELETE FROM recommendation_policies')
        publication.OWNER.publish(store.conn, stage=ready(store.conn))
        read = lambda s: {key: s.rec_proposals._proposal_json(key) for key in expected}
        selected = publication.OWNER.selected
    elif kind == 'listening':
        from tests.core.test_listening_output_preparation import seed
        from tests.core.test_listening_publication import prepared, mapped
        from yt_playlist.core.preparation import listening_outputs as preparation, listening_publication as publication
        expected = seed(store)
        prepared(store.conn)
        publication.OWNER.publish(store.conn, stage=mapped(store.conn).id)
        read = lambda s: {key: s.rec_proposals._proposal_json(key) for key in expected}
        selected = publication.OWNER.selected
    elif kind == 'documents':
        from tests.core.test_collection_document_preparation import seed
        from tests.core.test_collection_document_publication import stage
        from yt_playlist.core.preparation import collection_documents as preparation, collection_document_publication as publication
        _, _, recipe, canvas = seed(store)
        expected = {'recipe': recipe, 'canvas': canvas}
        for owner in publication.OWNERS.values():
            owner.publish(store.conn, stage=stage(owner, store.conn))
        read = lambda s: {'recipe': s.playlist_recipes.get_recipe('PLAYLIST'),
                          'canvas': s.playlist_recipes.get_cluster_canvas('PLAYLIST')}
        selected = lambda c: {key: owner.selected(c) for key, owner in publication.OWNERS.items()}
    elif kind == 'models':
        from tests.core.test_model_artifact_preparation import seed
        from tests.core.test_model_publication import prepare
        from yt_playlist.core.preparation import model_artifacts as preparation, model_publication as publication
        sources = seed(store)
        expected = {key: sources[key] for key in preparation.ARTIFACTS}
        publication.publish(store.conn, **prepare(store))
        read = lambda s: {key: s.settings.get_setting(key) for key in expected}
        selected = publication.selected
    else:
        from tests.core.test_report_preparation import seed
        from tests.core.test_report_publication import prepare
        from yt_playlist.core.preparation import recommendation_reports as preparation, report_publication as publication
        expected = seed(store)
        publication.publish(store.conn, revision=prepare(store.conn))
        read = lambda s: s.rec_proposals._proposal_json('listening_choices')
        selected = lambda c: publication.selected(c, 'listening_choices')
    assert read(store) == expected
    return preparation.directory(store.conn), read, selected


def hashes(root):
    # Lifetime locks are coordination state; every immutable data file is exact.
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*.json')}


def sql_custody(conn):
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' "
        "AND (name LIKE 'prepared_%' OR name LIKE 'published_%' OR name LIKE 'prepare_%') ORDER BY name")]
    result = {}
    for table in tables:
        columns = [row[1] for row in conn.execute('PRAGMA table_info(' + table + ')')
                   if row[1] not in {'filename', 'plan_filename', 'plan_digest', 'plan_bytes',
                                     'recovery_filename', 'retained_filename'}]
        result[table] = sorted(map(tuple, conn.execute('SELECT ' + ','.join(columns) + ' FROM ' + table)), key=repr)
    return result


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('kind', ['home', 'listening', 'documents', 'models', 'reports'])
@pytest.mark.parametrize('original', [False, True], ids=['compact', 'verified-backup'])
def test_selected_files_read_exactly_without_producer(installed, tmp_path, kind, original):
    producer, _ = installed
    root, read, selected = selected_owner(producer, kind)
    expected, selection = read(producer), selected(producer.conn)
    before, before_files, custody = list(producer.conn.iterdump()), hashes(root), sql_custody(producer.conn)
    report = policy_recovery.recover(producer._db_path, tmp_path / 'recovered', now=10000, progress=lambda _: None)
    assert list(producer.conn.iterdump()) == before and hashes(root) == before_files
    assert 'preparation' in report['verified_backup_file_manifests']
    assert not (tmp_path / 'recovered' / 'working.db.preparation').exists()
    with closing(sqlite3.connect(Path(report['verified_backup']).as_uri() + '?mode=ro', uri=True)) as backup:
        assert list(backup.iterdump()) == before
        path = tmp_path / 'restored.db' if original else Path(report['compact_database'])
        if original:
            with closing(sqlite3.connect(path)) as destination:
                backup.backup(destination)
    candidate = Store(path)
    hidden = root.with_name(root.name + '-unavailable')
    root.rename(hidden)
    try:
        if original:
            policy_recovery._restore_files(candidate.conn, report['verified_backup_file_manifests'])
        candidate.init_schema(defer_metadata=True)
        assert read(candidate) == expected
        assert selected(candidate.conn) == selection
        assert sql_custody(candidate.conn) == custody
        assert not candidate.conn.execute('PRAGMA foreign_key_check').fetchone()
        # Selected readers can conceal missing plans. Open every retained plan,
        # including its last indexed unit, while the original root is hidden.
        for owner in recovery.references(candidate.conn):
            if owner['columns'][0] == 'plan_filename':
                row = candidate.conn.execute('SELECT * FROM ' + owner['table'] + ' WHERE ' +
                    ' AND '.join(key + '=?' for key in owner['keys']), tuple(owner['keys'].values())).fetchone()
                surface = row['source'] if 'source' in row.keys() else row['surface']
                plan = document_plan.load(surface, owner['reference'])
                assert plan.units[len(plan.units) - 1]
    finally:
        candidate.conn.close()
        hidden.rename(root)
    assert list(producer.conn.iterdump()) == before and hashes(root) == before_files


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_interrupted_coordinate_relocation_restores_guards_heads_and_epochs(installed, tmp_path, monkeypatch):
    store, _ = installed
    root, _, _ = selected_owner(store, 'documents')
    with store.conn:
        store.conn.execute('BEGIN')
        manifest = recovery.export(store.conn, tmp_path / 'backup')
    before, before_files = list(store.conn.iterdump()), hashes(root)
    calls = 0
    original = recovery.check_cancellable_work
    def interrupt():
        nonlocal calls
        calls += 1
        # Cancellation during SQL relocation, after the guards are suspended.
        if calls == 2:
            raise InterruptedError('coordinate relocation')
        original()
    with monkeypatch.context() as patch:
        patch.setattr(recovery, 'check_cancellable_work', interrupt)
        with pytest.raises(InterruptedError, match='coordinate relocation'):
            recovery.restore(store.conn, manifest)
    assert not store.conn.in_transaction
    assert list(store.conn.iterdump()) == before and hashes(root) == before_files
    recovery.restore(store.conn, manifest)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [97], indirect=True)
@pytest.mark.parametrize('original', [False, True], ids=['compact', 'verified-backup'])
def test_native_operation_captures_survive_owner_rename_and_recovery(installed, tmp_path, original):
    from tests.core.test_append_operation_preparation import seed
    from yt_playlist.core.migrations import registry, runner
    from yt_playlist.core.preparation import append_operations as preparation
    producer, version = installed
    names, _, _, _ = seed(producer, version)
    for name in names:
        job = preparation.JOBS[name]
        job.install(producer.conn)
        job.step(producer.conn, batch_size=1)
    # Upgrade a separate copy through the real owner renames, retaining the
    # incomplete earlier custody. The producer's historical schema stays exact.
    upgraded = Store(tmp_path / 'upgraded.db')
    candidate = None
    root = producer._db_path.with_name(producer._db_path.name + '.preparation')
    try:
        producer.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:107])
        before = sql_custody(upgraded.conn)
        report = policy_recovery.recover(upgraded._db_path, tmp_path / 'recovered', now=10000, progress=lambda _: None)
        path = tmp_path / 'restored.db' if original else Path(report['compact_database'])
        if original:
            with closing(sqlite3.connect(report['verified_backup'])) as source:
                with closing(sqlite3.connect(path)) as destination:
                    source.backup(destination)
        candidate = Store(path)
        hidden = root.with_name(root.name + '-unavailable')
        root.rename(hidden)
        try:
            if original:
                policy_recovery._restore_files(candidate.conn, report['verified_backup_file_manifests'])
            candidate.init_schema(defer_metadata=True)
            assert sql_custody(candidate.conn) == before
            assert candidate.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 107
            for owner in recovery.references(candidate.conn):
                assert Path(owner['reference'][0]).is_relative_to(path.with_name(path.name + '.preparation'))
                recovery._read(owner['reference'])
                if owner['columns'][0] == 'plan_filename':
                    source = next(iter(owner['keys'].values()))
                    plan = document_plan.load(source, owner['reference'])
                    assert plan.units[len(plan.units) - 1]
            assert not candidate.conn.execute('PRAGMA foreign_key_check').fetchone()
        finally:
            hidden.rename(root)
    finally:
        if candidate:
            candidate.conn.close()
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [97], indirect=True)
def test_recovery_rejects_changed_preparation_owner_constraints(installed):
    from yt_playlist.core.preparation import append_operations as preparation
    store, _ = installed
    table, definition = next(iter(preparation.definitions('tv_native')))
    # Matching file columns are insufficient: the complete supported owner
    # definition, including its constraints, must still match exactly.
    store.conn.execute(definition.replace('part_count BETWEEN 1 AND 512', 'part_count BETWEEN 1 AND 513'))
    with pytest.raises(ValueError, match='Incompatible preparation file owner: ' + table):
        list(recovery.references(store.conn))


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_missing_plan_only_unit_cannot_report_success(installed, tmp_path):
    from tests.core.test_collection_document_preparation import seed
    from yt_playlist.core.preparation import collection_documents as preparation
    store, _ = installed
    seed(store, count=200)
    job = preparation.JOBS['recipes']
    job.install(store.conn)
    job.step(store.conn, batch_size=1)
    row = store.conn.execute('SELECT * FROM ' + preparation.table('recipes', 'generations')).fetchone()
    plan = document_plan.load(row['source'], tuple(row[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')))
    filename = Path(plan.units.indexes[-1][1])
    hidden = filename.with_suffix('.unavailable')
    before = list(store.conn.iterdump())
    filename.rename(hidden)
    try:
        with pytest.raises(FileNotFoundError):
            policy_recovery.recover(store._db_path, tmp_path / 'failed', progress=lambda _: None)
        assert not (tmp_path / 'failed' / 'report.json').exists()
        assert list(store.conn.iterdump()) == before
    finally:
        hidden.rename(filename)


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('kind', ['taste', 'workflow', 'actions', 'ranked', 'notes',
                                  'genres', 'conflicts', 'recipes', 'library', 'playback', 'origins', 'learning', 'feedback',
                                  'drafts', 'shares', 'discovery', 'tv', 'append', 'radio'])
@pytest.mark.parametrize('original', [False, True], ids=['compact', 'verified-backup'])
def test_unfinished_nested_plans_and_backups_resume_without_producer(installed, tmp_path, kind, original):
    store, _ = installed
    if kind == 'taste':
        from tests.core.test_taste_control_preparation import seed
        from yt_playlist.core.preparation import taste_controls as preparation
    elif kind == 'workflow':
        from tests.core.test_workflow_receipt_preparation import seed
        from yt_playlist.core.preparation import workflow_receipts as preparation
    elif kind == 'actions':
        from tests.core.test_action_ledger_preparation import seed
        from yt_playlist.core.preparation import action_ledger as preparation
    elif kind == 'ranked':
        from tests.core.test_ranked_state_preparation import seed
        from yt_playlist.core.preparation import ranked_state as preparation
    elif kind == 'notes':
        from tests.core.test_album_note_preparation import seed
        from yt_playlist.core.preparation import album_notes as preparation
    elif kind == 'genres':
        import json
        from tests.core.test_genre_preference_preparation import seed as catalog, BASELINE
        from yt_playlist.core.preparation import genre_preferences as preparation
        def seed(s):
            catalog(s.conn)
            with s.conn:
                s.conn.executemany('INSERT INTO settings VALUES (?,?)',
                    [('genre_defaults', json.dumps(BASELINE, indent=2)), ('genres_seeded', '0')])
    elif kind == 'conflicts':
        from tests.core.test_metadata_conflict_preparation import seed
        from yt_playlist.core.preparation import metadata_conflicts as preparation
    elif kind == 'recipes':
        from tests.core.test_recipe_membership_preparation import seed
        from yt_playlist.core.preparation import recipe_memberships as preparation
    elif kind == 'library':
        from tests.core.test_library_sync_preparation import seed
        from yt_playlist.core.preparation import library_sync as preparation
    elif kind == 'playback':
        from tests.core.test_playback_context_preparation import seed
        from yt_playlist.core.preparation import playback_context as preparation
    elif kind == 'origins':
        from tests.core.test_radio_origin_preparation import seed
        from yt_playlist.core.preparation import radio_origins as preparation
    elif kind == 'feedback':
        from tests.core.test_collection_feedback_preparation import seed
        from yt_playlist.core.preparation import collection_feedback as preparation
    elif kind == 'drafts':
        from tests.core.test_road_trip_draft_preparation import seed
        from yt_playlist.core.preparation import road_trip_drafts as preparation
    elif kind == 'shares':
        from tests.core.test_album_share_publication import complete_source as seed
        from yt_playlist.core.preparation import album_shares as preparation
    elif kind == 'discovery':
        from tests.core.test_discovery_owner_preparation import seed
        from yt_playlist.core.preparation import discovery_artists as preparation
    elif kind == 'tv':
        from tests.core.test_tv_state_preparation import seed as proposals, aliases
        from yt_playlist.core.preparation import tv_state as preparation
        def seed(s):
            proposals(s)
            aliases(s.conn)
    elif kind == 'append':
        from tests.core.test_append_operation_preparation import seed as operations
        from yt_playlist.core.preparation import append_operations as preparation
        seed = lambda s: operations(s, 79)
    elif kind == 'radio':
        from tests.core.test_append_operation_preparation import seed as operations
        from yt_playlist.core.preparation import radio_session as preparation
        def seed(s):
            import json
            _, _, _, state = operations(s, 79, append=True)
            state['tracks'] = [{'video_id': 'same', 'title': 'Captured', 'artist': 'Artist'}] * 3
            s.settings.set_setting(preparation.KEY, json.dumps(state, indent=2))
    else:
        from tests.core.test_choice_learning_preparation import seed
        from yt_playlist.core.preparation import choice_learning as preparation
    seed(store)
    if kind == 'genres':
        jobs = [preparation.DEFAULTS]
    elif kind == 'library':
        jobs = [preparation.RESULT_JOB]
    elif kind == 'playback':
        jobs = [preparation.BINDINGS_JOB, preparation.PENDING_JOB]
    elif kind == 'feedback':
        jobs = [preparation.FEEDBACK_JOB, preparation.AUTH_JOB]
    elif kind == 'discovery':
        jobs = [preparation.JOBS['artists_legacy']]
    elif kind == 'append':
        jobs = [preparation.JOBS['tv_legacy'], preparation.JOBS['radio_legacy']]
    elif kind in {'taste', 'workflow', 'ranked', 'tv'}:
        jobs = list(preparation.JOBS.values())
    else:
        jobs = [preparation.JOB]
    for job in jobs:
        job.install(store.conn)
        assert job.step(store.conn, batch_size=1).status != 'prepared'
    # A whole preparation tree is unavailable, including pages not yet in SQL.
    root = store._db_path.with_name(store._db_path.name + '.preparation')
    custody, before, before_files = sql_custody(store.conn), list(store.conn.iterdump()), hashes(root)
    report = policy_recovery.recover(store._db_path, tmp_path / 'recovered', now=10000, progress=lambda _: None)
    path = tmp_path / 'restored.db' if original else Path(report['compact_database'])
    if original:
        with closing(sqlite3.connect(report['verified_backup'])) as source:
            with closing(sqlite3.connect(path)) as target:
                source.backup(target)
    candidate = Store(path)
    hidden = root.with_name(root.name + '-unavailable')
    root.rename(hidden)
    try:
        if original:
            policy_recovery._restore_files(candidate.conn, report['verified_backup_file_manifests'])
        candidate.init_schema(defer_metadata=True)
        assert sql_custody(candidate.conn) == custody
        for owner in recovery.references(candidate.conn):
            assert Path(owner['reference'][0]).is_relative_to(path.with_name(path.name + '.preparation'))
            recovery._read(owner['reference'])
        for job in jobs:
            for _ in range(100):
                if job.step(candidate.conn, batch_size=2).status == 'prepared':
                    break
            else:
                pytest.fail('Recovered preparation did not finish: ' + job.name)
        for owner in recovery.references(candidate.conn):
            assert Path(owner['reference'][0]).is_relative_to(path.with_name(path.name + '.preparation'))
            recovery._read(owner['reference'])
        if kind == 'ranked':
            # Relocation must preserve capture for later real policy changes.
            revisions = [job.progress(candidate.conn).input_revision for job in jobs]
            candidate.rec_ledger.record_serving_policy('later-policy', {'algorithm': 'changed'}, 2)
            assert [job.progress(candidate.conn).input_revision for job in jobs] == [value + 1 for value in revisions]
        assert not candidate.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        candidate.conn.close()
        hidden.rename(root)
    assert list(store.conn.iterdump()) == before and hashes(root) == before_files
