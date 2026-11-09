"""Exercise leased compact builds, interruption and shared-content ownership."""
from contextlib import ExitStack, closing, contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event

import pytest

from tests.core.test_compact_groups_migration import historical_groups, apply
from tests.genre_catalog_support import bound_catalog
from tests.prepared_fixture_support import capture_template, roots
from yt_playlist.core.activity import WorkClosing
from yt_playlist.core import group_content, group_documents, group_plan, group_reads as group_queries, group_storage
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.migrations import v072_compact_groups as migration
from yt_playlist.core.migrations import registry, runner
from yt_playlist.core.store import Store
from yt_playlist.repos import compact_group_publication as compact, group_publication as publication
from yt_playlist.repos.compact_taste_groups import CompactTasteGroupsRepo


def third_generation(conn):
    prepared = migration._prepared(conn, conn.execute(
        "SELECT * FROM rec_group_generations WHERE generation='second'").fetchone())
    prepared.update(generation='third', at=3, serial=3, _stage_parent='second')
    prepared['unknown'] = [('unknown-old-key', 'unmeasured')]
    prepared['edges'] = [('one', 'two', .8, .7), ('one', 'unknown-old-key', .6, .5)]
    prepared['lineage'] = [('second-leaf', 'third-leaf', 'continuation',
                            '{"old_share":1,"new_share":1,"overlap":1,"compatible":true}')]
    for group in prepared['groups']:
        group['version'] = group['version'].replace('second-', 'third-')
        if group['parent_version']:
            group['parent_version'] = group['parent_version'].replace('second-', 'third-')
    return prepared


def bind_compact_store(store):
    store._bind_repositories()
    store.conn.execute('PRAGMA journal_mode=WAL')
    store._snapshot_revisions = Revisions(store.conn)


@contextmanager
def compacted_groups(pristine, path):
    with ExitStack() as resources:
        home = resources.enter_context(TemporaryDirectory(prefix='tuneconsole-compact-template-'))
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv('YT_PLAYLIST_HOME', home)
            patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
            patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
            patch.delenv('DEEPSEEK_API_KEY', raising=False)
            with historical_groups(path, pristine) as conn:
                prepared = third_generation(conn)
                apply(conn)
            # Close the predecessor before retaining the immutable source. Do
            # not admit this historical schema through current-schema startup.
            source = Store(path)
            resources.callback(source.conn.close)
            bind_compact_store(source)
            template = capture_template(source, prepared)
        yield template


@contextmanager
def restored_groups(template, path):
    store = Store(path)
    try:
        prepared = template.restore(store)
        store._bind_repositories()
        assert store.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        assert store._snapshot_revisions.conn is store.conn
        yield store, prepared
    finally:
        store.conn.close()


@pytest.fixture(scope='module')
def bounded_compact_group_template(tmp_path_factory):
    """Replay the real compact-group input with its one required genre."""
    path = tmp_path_factory.mktemp('retention-group-input') / 'source.db'
    with ExitStack() as resources:
        with pytest.MonkeyPatch.context() as patch, closing(sqlite3.connect(':memory:')) as source:
            bound_catalog(patch, entries=(('house', 'House'),))
            source.execute('PRAGMA foreign_keys=ON')
            runner.migrate(source, registry=registry.MIGRATIONS[:69])
            source.execute('PRAGMA query_only=ON')
            template = resources.enter_context(compacted_groups(source, path))
        yield template


@pytest.fixture
def database(tmp_path, compact_group_template):
    with restored_groups(compact_group_template, tmp_path / 'groups.db') as result:
        yield result


def changed(prepared):
    prepared = deepcopy(prepared)
    # Change one member's semantic weight, leaving the other group reusable.
    prepared['groups'][1]['members'][0] = ('one', .5, .8)
    return prepared


def finish(store, prepared, owner):
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        compact.finish(store.conn, prepared['generation'], owner)


def test_unchanged_leased_preparation_reuses_content_and_preserves_each_generation(database):
    store, prepared = database
    original = [tuple(row) for row in store.conn.execute('SELECT * FROM group_members')]
    owner = store.taste_groups.stage(prepared)
    assert group_queries.head_generation(store.conn) == 'second'
    assert group_queries.definitions(store.conn, 'third') == []
    assert store.conn.execute('SELECT count(*) FROM group_versions').fetchone()[0] == 2
    assert [tuple(row) for row in store.conn.execute('SELECT * FROM group_members')] == original
    finish(store, prepared, owner)
    assert group_queries.head_generation(store.conn) == 'third'
    assert group_queries.definition(store.conn, 'second-leaf', generation='second')['parent_version'] == 'second-parent'
    assert group_queries.definition(store.conn, 'third-leaf', generation='third')['parent_version'] == 'third-parent'
    assert group_queries.definition(store.conn, 'second-leaf', generation='third') is None
    assert group_queries.canonical_version(store.conn, 'second-leaf') == group_queries.canonical_version(store.conn, 'third-leaf')
    assert not store.conn.execute('SELECT 1 FROM group_builds').fetchone()
    assert not store.conn.execute('SELECT 1 FROM group_build_versions').fetchone()
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def interrupt_after_member_batch(monkeypatch):
    original, interrupted = publication._write, False

    @contextmanager
    def write(repo):
        nonlocal interrupted
        with original(repo) as conn:
            yield conn
        if not interrupted and repo.conn.execute('SELECT 1 FROM group_build_versions b JOIN group_members m '
                'ON m.group_version_id=b.group_version_id LIMIT 1').fetchone():
            interrupted = True
            raise WorkClosing('controlled interruption after committed member batch')
    monkeypatch.setattr(publication, '_write', write)
    return original


def test_committed_prefix_resumes_without_duplicate_members_or_early_visibility(database, monkeypatch):
    store, prepared = database
    prepared = changed(prepared)
    original = interrupt_after_member_batch(monkeypatch)
    with pytest.raises(WorkClosing, match='controlled interruption'):
        store.taste_groups.stage(prepared)
    assert group_queries.head_generation(store.conn) == 'second'
    assert group_queries.definition(store.conn, 'third-leaf') is None
    assert store.conn.execute('SELECT count(*) FROM group_build_versions').fetchone()[0] == 1
    with pytest.raises(RuntimeError, match='incomplete or no longer owned'):
        finish(store, prepared, prepared['_stage_owner'])
    monkeypatch.setattr(publication, '_write', original)
    owner = store.taste_groups.stage(prepared)
    finish(store, prepared, owner)
    assert store.conn.execute('SELECT count(*) FROM group_versions').fetchone()[0] == 3
    assert store.conn.execute('SELECT count(*) FROM group_members').fetchone()[0] == 4
    assert group_queries.head(store.conn, members=True)['groups'][1]['members'] == {'one': (.5, .8)}


def test_committed_groups_resume_read_only_after_store_restart_with_ownership_checks(database, monkeypatch):
    store, prepared = database
    original = CompactTasteGroupsRepo.link_group
    def interrupt(repo, *args):
        original(repo, *args)
        raise WorkClosing('interrupted after committed group')
    monkeypatch.setattr(CompactTasteGroupsRepo, 'link_group', interrupt)
    with pytest.raises(WorkClosing, match='committed group'):
        store.taste_groups.stage(prepared)
    store.conn.close()
    restarted = Store(store._db_path)
    with closing(restarted.conn):
        bind_compact_store(restarted)
        repository = restarted.taste_groups.repository
        identities = repository.recording_ids(group_plan.recording_keys(prepared))
        _inputs, plans, _policy, _metadata = group_plan.prepare(prepared, identities)
        generation = restarted.conn.execute("SELECT id FROM group_generations WHERE external_key='third'").fetchone()[0]
        owner = prepared['_stage_owner']
        before = restarted.conn.total_changes
        assert repository.group_state(generation, owner, plans[0]) is None
        assert repository.selected_groups(generation, owner) == {
            prepared['groups'][0]['group_id']: prepared['groups'][0]['version']}
        assert restarted.conn.total_changes == before
        with pytest.raises(RuntimeError, match='no longer owned'):
            repository.group_state(generation, 'another-worker', plans[0])
        with pytest.raises(RuntimeError, match='no longer owned'):
            repository.selected_groups(generation, 'another-worker')
        conflicting = deepcopy(plans[0])
        conflicting[1]['version'] = 'different-alias'
        with pytest.raises(ValueError, match='staged selection changed'):
            repository.group_state(generation, owner, conflicting)
        assert group_queries.head_generation(restarted.conn) == 'second'
        resumed = deepcopy(prepared)
        resumed.pop('_stage_owner')
        with pytest.raises(publication.BuildUnavailable, match='leased by another worker'):
            restarted.taste_groups.stage(resumed)
        with restarted.conn:
            restarted.conn.execute('UPDATE group_builds SET expires_at=0 WHERE generation_id=?', (generation,))
        monkeypatch.setattr(CompactTasteGroupsRepo, 'link_group', original)
        next_owner = restarted.taste_groups.stage(resumed)
        assert next_owner != owner
        finish(restarted, resumed, next_owner)
        assert group_queries.head_generation(restarted.conn) == 'third'


def test_live_lease_blocks_takeover_and_expired_lease_fences_the_old_owner(database, monkeypatch):
    store, prepared = database
    prepared = changed(prepared)
    original = interrupt_after_member_batch(monkeypatch)
    with pytest.raises(WorkClosing):
        store.taste_groups.stage(prepared)
    monkeypatch.setattr(publication, '_write', original)
    other = deepcopy(prepared)
    old_owner = other.pop('_stage_owner')
    with pytest.raises(publication.BuildUnavailable, match='leased by another worker'):
        store.taste_groups.stage(other)
    with store.conn:
        store.conn.execute('UPDATE group_builds SET expires_at=0')
    new_owner = store.taste_groups.stage(other)
    assert new_owner != old_owner
    with pytest.raises(RuntimeError, match='incomplete or no longer owned'):
        finish(store, prepared, old_owner)
    finish(store, other, new_owner)
    assert group_queries.head_generation(store.conn) == 'third'


def test_graph_retry_skips_committed_group_batches(database, monkeypatch):
    store, prepared = database
    original = CompactTasteGroupsRepo.install_caches
    def interrupt(*args):
        raise WorkClosing('interrupted before graph publication')
    monkeypatch.setattr(CompactTasteGroupsRepo, 'install_caches', interrupt)
    with pytest.raises(WorkClosing, match='before graph publication'):
        store.taste_groups.stage(prepared)
    assert store.conn.execute("SELECT count(*) FROM generation_groups x JOIN group_generations g "
                              "ON g.id=x.generation_id WHERE g.external_key='third'").fetchone()[0] == len(prepared['groups'])
    monkeypatch.setattr(CompactTasteGroupsRepo, 'install_caches', original)
    def revisit(*args):
        pytest.fail('Graph retry revisited a committed group batch')
    monkeypatch.setattr(CompactTasteGroupsRepo, 'definition_candidates', revisit)
    monkeypatch.setattr(CompactTasteGroupsRepo, 'group_state', revisit)
    owner = store.taste_groups.stage(prepared)
    assert group_queries.head_generation(store.conn) == 'second'
    finish(store, prepared, owner)
    assert group_queries.head_generation(store.conn) == 'third'


def test_expired_cleanup_removes_only_the_abandoned_build_and_can_rebuild(database, monkeypatch):
    store, prepared = database
    prepared = changed(prepared)
    original = interrupt_after_member_batch(monkeypatch)
    with pytest.raises(WorkClosing):
        store.taste_groups.stage(prepared)
    monkeypatch.setattr(publication, '_write', original)
    assert not store.taste_groups.cleanup_builds()
    with store.conn:
        store.conn.execute('UPDATE group_builds SET expires_at=0')
    monkeypatch.setattr(publication, 'BATCH_ROWS', 1)
    for _ in range(100):
        if not store.taste_groups.cleanup_builds():
            break
        assert group_queries.head_generation(store.conn) == 'second'
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    else:
        pytest.fail('Compact cleanup did not finish its bounded deletions')
    assert store.conn.execute('SELECT count(*) FROM group_versions').fetchone()[0] == 2
    assert store.conn.execute('SELECT count(*) FROM group_members').fetchone()[0] == 3
    assert not store.conn.execute("SELECT 1 FROM group_generations WHERE external_key='third'").fetchone()
    owner = store.taste_groups.stage(prepared)
    finish(store, prepared, owner)
    assert group_queries.head_generation(store.conn) == 'third'


@pytest.mark.parametrize('complete', [False, True])
@pytest.mark.parametrize('interrupted', [False, True])
def test_publication_during_partial_orphan_manifest_cleanup(database, monkeypatch, interrupted, complete):
    store, prepared = database
    payload = json.loads(prepared['payload'])
    payload['recording_inputs']['one'] = '4' * 64
    prepared['payload'] = json.dumps(payload)
    identities = dict(store.conn.execute('SELECT identity_key,id FROM recordings'))
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        if complete:
            manifest = group_storage.install_manifest(store.conn, payload['recording_inputs'], identities.get)
        else:
            rows = group_storage.normalized_inputs(payload['recording_inputs'], identities.get)
            digest = group_content.digest(group_content.canonical([owner, value]) for owner, value in rows)
            manifest = store.conn.execute('INSERT INTO group_input_manifests(content_digest) VALUES (?) '
                                          'RETURNING id', (digest,)).fetchone()[0]
            group_storage.insert_inputs(store.conn, manifest, rows)
    exact = list(group_storage.manifest_rows(store.conn, manifest))
    historical = group_queries.head(store.conn, members=True)
    monkeypatch.setattr(publication, 'BATCH_ROWS', 1)
    assert store.taste_groups.cleanup_builds()
    assert 0 < len(list(group_storage.manifest_rows(store.conn, manifest))) < len(exact)
    if interrupted:
        original = compact.cleanup
        def cleanup(repo, **kwargs):
            original(repo, **kwargs)
            raise WorkClosing('interrupted orphan manifest retirement')
        monkeypatch.setattr(compact, 'cleanup', cleanup)
        with pytest.raises(WorkClosing, match='orphan manifest retirement'):
            store.taste_groups.stage(prepared)
        assert group_queries.head(store.conn, members=True) == historical
        monkeypatch.setattr(compact, 'cleanup', original)
    owner = store.taste_groups.stage(prepared)
    assert group_queries.head(store.conn, members=True) == historical
    finish(store, prepared, owner)
    selected = store.conn.execute("SELECT input_manifest_id FROM group_generations WHERE external_key='third'").fetchone()[0]
    assert list(group_storage.manifest_rows(store.conn, selected)) == exact
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_unchanged_complete_orphan_manifest_is_reused(database):
    store, prepared = database
    payload = json.loads(prepared['payload'])
    payload['recording_inputs']['one'] = '4' * 64
    prepared['payload'] = json.dumps(payload)
    identities = dict(store.conn.execute('SELECT identity_key,id FROM recordings'))
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        manifest = group_storage.install_manifest(store.conn, payload['recording_inputs'], identities.get)
    exact = list(group_storage.manifest_rows(store.conn, manifest))
    owner = store.taste_groups.stage(prepared)
    selected = store.conn.execute("SELECT input_manifest_id FROM group_generations WHERE external_key='third'").fetchone()[0]
    assert selected == manifest
    assert list(group_storage.manifest_rows(store.conn, selected)) == exact
    finish(store, prepared, owner)


@pytest.mark.parametrize('unowned', [False, True])
def test_input_manifest_digest_match_still_rejects_conflicting_content(database, monkeypatch, unowned):
    store, prepared = database
    payload = json.loads(prepared['payload'])
    payload['recording_inputs']['one'] = '4' * 64
    prepared['payload'] = json.dumps(payload)
    identities = dict(store.conn.execute('SELECT identity_key,id FROM recordings'))
    expected = [group_content.canonical([owner, value]) for owner, value in
                group_storage.normalized_inputs(payload['recording_inputs'], identities.get)]
    if unowned:
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            manifest = group_storage.install_manifest(store.conn, {'one': '5' * 64}, identities.get)
        digest = store.conn.execute('SELECT content_digest FROM group_input_manifests WHERE id=?', (manifest,)).fetchone()[0]
    else:
        digest = store.conn.execute('SELECT content_digest FROM group_input_manifests').fetchone()[0]
    real_digest = group_content.digest
    def collide(rows):
        rows = list(rows)
        return digest if rows == expected else real_digest(rows)
    monkeypatch.setattr(group_content, 'digest', collide)
    before = list(store.conn.iterdump())
    with pytest.raises(ValueError, match='Musical group input digest collision'):
        store.taste_groups.stage(prepared)
    assert list(store.conn.iterdump()) == before


def test_referenced_incomplete_input_manifest_cannot_be_reused(database, monkeypatch):
    store, prepared = database
    payload = json.loads(prepared['payload'])
    payload['recording_inputs']['one'] = '4' * 64
    prepared['payload'] = json.dumps(payload)
    original = CompactTasteGroupsRepo.install_inputs
    def interrupt(*args):
        raise WorkClosing('interrupted before manifest inputs')
    monkeypatch.setattr(CompactTasteGroupsRepo, 'install_inputs', interrupt)
    with pytest.raises(WorkClosing, match='before manifest inputs'):
        store.taste_groups.stage(prepared)
    other = deepcopy(prepared)
    other.update(generation='fourth', at=4, serial=4)
    other.pop('_stage_owner')
    before = list(store.conn.iterdump())
    with pytest.raises(publication.BuildUnavailable, match='inputs are leased'):
        store.taste_groups.stage(other)
    assert list(store.conn.iterdump()) == before
    monkeypatch.setattr(CompactTasteGroupsRepo, 'install_inputs', original)
    owner = store.taste_groups.stage(prepared)
    finish(store, prepared, owner)
    assert group_queries.head_generation(store.conn) == 'third'


@pytest.mark.parametrize('interrupt_cleanup', [False, True])
def test_new_plan_clears_expired_foreign_aliases_with_bounded_restartable_cleanup(database, monkeypatch,
                                                                                interrupt_cleanup):
    store, prepared = database
    abandoned_owner = store.taste_groups.stage(prepared)
    historical = group_queries.head(store.conn, members=True)
    next_plan = deepcopy(prepared)
    next_plan.update(generation='fourth', at=4, serial=4)
    next_plan.pop('_stage_owner')
    with store.conn:
        store.conn.execute('UPDATE group_builds SET expires_at=0')
    monkeypatch.setattr(publication, 'BATCH_ROWS', 1)
    original = compact.cleanup
    calls = []

    def cleanup(repo, **kwargs):
        changed = original(repo, **kwargs)
        calls.append(kwargs)
        assert group_queries.head(repo.conn, members=True) == historical
        assert not repo.conn.execute('PRAGMA foreign_key_check').fetchall()
        if interrupt_cleanup and len(calls) == 1:
            raise WorkClosing('interrupted foreign build cleanup')
        return changed

    monkeypatch.setattr(compact, 'cleanup', cleanup)
    if interrupt_cleanup:
        with pytest.raises(WorkClosing, match='interrupted foreign build cleanup'):
            store.taste_groups.stage(next_plan)
        assert not store.conn.execute("SELECT 1 FROM group_generations WHERE external_key='fourth'").fetchone()
    owner = store.taste_groups.stage(next_plan)
    assert calls and all(call == {'generation': 'third'} for call in calls)
    assert not store.conn.execute("SELECT 1 FROM group_generations WHERE external_key='third'").fetchone()
    assert group_queries.head(store.conn, members=True) == historical
    with pytest.raises(RuntimeError, match='Missing staged'):
        finish(store, prepared, abandoned_owner)
    finish(store, next_plan, owner)
    assert group_queries.head_generation(store.conn) == 'fourth'
    assert group_queries.head(store.conn, members=True)['groups'][1]['members'] == historical['groups'][1]['members']


def test_new_plan_preserves_live_foreign_alias_owner(database):
    store, prepared = database
    owner = store.taste_groups.stage(prepared)
    next_plan = deepcopy(prepared)
    next_plan.update(generation='fourth', at=4, serial=4)
    next_plan.pop('_stage_owner')
    before = list(store.conn.iterdump())
    with pytest.raises(publication.BuildUnavailable, match='leased by another unpublished build'):
        store.taste_groups.stage(next_plan)
    assert list(store.conn.iterdump()) == before
    finish(store, prepared, owner)
    assert group_queries.head_generation(store.conn) == 'third'


@pytest.mark.parametrize('change', ['none', 'recording', 'external', 'head'])
def test_hidden_append_preserves_the_original_publication_guard(database, change):
    store, prepared = database
    with store.read_snapshot() as snapshot:
        assert group_queries.head(snapshot.store.conn, members=True)['generation'] == 'second'
        snapshot.release_reader()
        owner = store.taste_groups.stage(prepared)
        if change == 'recording':
            # The outer preparation declares the exact input identities too.
            snapshot.store._snapshot_reads.include({('recordings', '')})
            with store.conn:
                store.conn.execute("UPDATE recordings SET title='changed input' WHERE identity_key='one'")
        elif change == 'external':
            import sqlite3
            with closing(sqlite3.connect(store._db_path)) as external, external:
                external.execute("INSERT INTO settings VALUES ('external-change','1')")
        elif change == 'head':
            with store.conn:
                store.conn.execute('UPDATE group_head SET generation_id=(SELECT id FROM group_generations '
                                   "WHERE external_key='first')")
        published = snapshot.publish(lambda: compact.finish(store.conn, prepared['generation'], owner),
                                     allow_stale_tables=compact.STAGED_TABLES)
        assert published == (change == 'none')
    assert group_queries.head_generation(store.conn) == ('third' if change == 'none' else
                                                        'first' if change == 'head' else 'second')


def test_resume_rejects_changed_plan_before_extending_the_prefix(database, monkeypatch):
    store, prepared = database
    prepared = changed(prepared)
    original = interrupt_after_member_batch(monkeypatch)
    with pytest.raises(WorkClosing):
        store.taste_groups.stage(prepared)
    monkeypatch.setattr(publication, '_write', original)
    altered = deepcopy(prepared)
    altered.pop('_stage_digest')
    value = json.loads(altered['groups'][1]['payload'])
    value['name'] = 'Changed output under the same generation identity'
    altered['groups'][1]['payload'] = json.dumps(value)
    before = list(store.conn.iterdump())
    with pytest.raises(RuntimeError, match='retained output digest'):
        store.taste_groups.stage(altered)
    assert list(store.conn.iterdump()) == before


def test_foreground_can_read_and_write_between_compact_member_batches(database, monkeypatch):
    store, prepared = database
    prepared = changed(prepared)
    paused, release = Event(), Event()
    original, stopped = publication._write, False

    @contextmanager
    def write(repo):
        nonlocal stopped
        with original(repo) as conn:
            yield conn
        if not stopped and repo.conn.execute('SELECT 1 FROM group_build_versions b JOIN group_members m '
                'ON m.group_version_id=b.group_version_id LIMIT 1').fetchone():
            stopped = True
            paused.set()
            assert release.wait(5)
    monkeypatch.setattr(publication, '_write', write)
    with ThreadPoolExecutor(2) as pool:
        work = pool.submit(store.taste_groups.stage, prepared)
        try:
            assert paused.wait(5)

            def foreground():
                store.settings.set_setting('foreground-during-build', 'responsive')
                with store.read_view() as view:
                    return group_queries.head(view.conn, members=True)

            head = pool.submit(foreground).result(2)
            assert head['generation'] == 'second'
            assert head['groups'][1]['members'] == {'one': (.9, .8)}
        finally:
            release.set()
        owner = work.result(5)
    finish(store, prepared, owner)
    assert store.settings.get_setting('foreground-during-build') == 'responsive'


def test_store_retains_group_files_until_the_last_old_snapshot_read(database, tmp_path, compact_group_template):
    store, prepared = database
    template = compact_group_template
    with restored_groups(template, tmp_path / 'independent.db') as (peer, peer_plan):
        assert prepared == peer_plan == template.result
        assert len({id(store.conn), id(peer.conn), id(template.source.conn)}) == 3
        assert len({store._evidence_key, peer._evidence_key, template.source._evidence_key}) == 3
        assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
        source_sql = list(template.source.conn.iterdump())
        destinations = [roots(target.conn) for target in (store, peer)]
        referenced = {Path(row[3]) for row in group_documents.references(template.source.conn)}
        retained = []
        for name, directory in template.files.items():
            for source in directory.rglob('*'):
                if not source.is_file():
                    continue
                paths = [source, *(owned[name] / source.relative_to(directory) for owned in destinations)]
                exact = source.read_bytes()
                assert all(path.read_bytes() == exact for path in paths)
                assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 3
                if exact and source in referenced:
                    retained.append((paths, exact))
        assert retained
        # The publication and reader operations below must leave both other
        # owners usable, including the immutable source for subsequent tests.
        prepared['groups'][0]['members'][0] = ('one', .1, .2)
        store.settings.set_setting('private-compact-fixture', 'first clone')
        expected = group_queries.head(peer.conn, members=True)
        # Give one owner a pack with an unowned record. Restoring an already compact
        # pack can reuse its path, so it would not exercise retirement of old files.
        table, key, kind, filename, digest, size, offset = next(group_documents.references(store.conn))
        assert table == 'group_versions'
        value, age = group_documents.read(filename, digest, size, offset, kind, with_age=True)
        replacement = next(group_documents.publish_many(group_documents.directory(store.conn),
            [(kind, value), ('definition', {'unowned': True})], age=age))
        with store.conn:
            store.conn.execute('UPDATE group_versions SET definition_filename=?,definition_offset=? WHERE id=?',
                               (replacement[0], replacement[3], key[0]))
        group_documents.collect_orphans(store.conn)
        with store.read_snapshot() as snapshot:
            view = snapshot.store
            original = list(group_documents.references(view.conn))
            manifest = group_documents.export(view.conn, tmp_path / 'pinned-backup')
            group_documents.restore(store.conn, manifest)
            # Restore repacks all owned records. The pinned WAL reader still sees
            # the previous files, which are now absent from current SQL owners.
            current = {row[3] for row in group_documents.references(store.conn)}
            old_files = {row[3] for row in original} - current
            assert old_files
            assert group_documents.collect_orphans(store.conn) == 0
            for _table, _key, kind, filename, digest, size, offset in original:
                group_documents.read(filename, digest, size, offset, kind)
        assert group_documents.collect_orphans(store.conn) == len(old_files)
        assert not any(Path(filename).exists() for filename in old_files)
        assert list(template.source.conn.iterdump()) == source_sql
        assert peer_plan == template.result
        assert group_queries.head(peer.conn, members=True) == expected
        assert group_queries.head(store.conn, members=True) == expected
        assert peer.settings.get_setting('private-compact-fixture') is None
        assert template.source.settings.get_setting('private-compact-fixture') is None
        paths, exact = retained[0]
        paths[2].write_bytes(b'Private peer file corruption')
        assert paths[0].read_bytes() == exact
        assert group_queries.head(store.conn, members=True) == expected


def test_compact_publication_preserves_membership_and_representatives(database):
    store, prepared = database
    repo = group_queries.service(store)
    repo.publish(prepared)
    assert repo.head_generation() == 'third'
    assert repo.head_checkpoint()['generation'] == 'third'
    assert repo.head(members=True)['groups'][1]['members'] == {'one': (.9, .8)}
    assert repo.head(members=True)['groups'][1]['rep_keys'] == ['one']
    assert not repo.expired_builds()
    assert repo.resumable_build('input-fingerprint', 3, prepared['policy'], 'second') is None


def test_group_publication_file_io_runs_outside_the_sql_transaction(database, monkeypatch):
    store, prepared = database
    files = store.taste_groups.files

    def checked(original):
        def outside(*args, **kwargs):
            assert not store.conn.in_transaction, 'File I/O ran inside a SQL transaction'
            return original(*args, **kwargs)
        return outside

    for method in ('write', 'write_policy', 'read', 'policy'):
        monkeypatch.setattr(files, method, checked(getattr(files, method)))
    store.taste_groups.publish(prepared)
    assert store.taste_groups.head_generation() == 'third'
    assert not store.conn.in_transaction


def test_real_offer_learning_reuses_the_same_external_alias_in_two_generations(database):
    from tests.test_group_learning import menu, compiled
    from yt_playlist.rec import choice_evidence as choices
    store, prepared = database
    store.taste_groups = group_queries.service(store)
    first = store.taste_groups.head(members=True)
    for group in prepared['groups']:
        group['version'] = group['version'].replace('third-', 'second-')
        if group['parent_version']:
            group['parent_version'] = group['parent_version'].replace('third-', 'second-')
    prepared['lineage'] = []
    for index in range(2):
        if index:
            owner = store.taste_groups.stage(prepared)
            finish(store, prepared, owner)
        head = store.taste_groups.head(members=True)
        at = 1100 + index * 86400
        oid = menu(store, head, ['leaf'], at=at, oid=f'actual-shared-alias-{index}')
        choices.respond(store, oid, 'visible', at + 1, visible=['leaf'])
        choices.respond(store, oid, 'choice', at + 2, option_id='leaf')
    current = store.taste_groups.head(members=True)
    assert current['generation'] != first['generation']
    assert current['groups'][1]['version'] == first['groups'][1]['version'] == 'second-leaf'
    assert store.taste_groups.definition('second-leaf', generation='second')['created_at'] == 2
    assert store.taste_groups.definition('second-leaf', generation='third')['created_at'] == 3
    state, rows = compiled(store, current, 1103 + 86400)
    assert rows['leaf']['positive'] == .5 and rows['leaf']['days'] == 2
    assert not any(state['excluded'].values())
    assert store.conn.execute('SELECT count(*) FROM group_versions').fetchone()[0] == 2


def test_group_repository_detaches_file_references_without_io_or_a_reporting_transaction(database, monkeypatch):
    from yt_playlist.core import policy_documents
    store, _ = database
    with store.read_view() as view:
        repository = CompactTasteGroupsRepo(view)
        statements = []
        view.conn.set_trace_callback(statements.append)

        def forbidden(*args, **kwargs):
            pytest.fail('The SQL repository attempted to read a file')

        with monkeypatch.context() as patch:
            patch.setattr(group_documents, 'read', forbidden)
            patch.setattr(policy_documents, 'read', forbidden)
            head = repository.head(members=True)
            assert head['groups'][1]['members'] == {'one': (.9, .8)}
            assert head['metadata_filename']
            assert repository.head_checkpoint()['policy']['variant']
            assert repository.definitions('second')[0]['definition_filename']
            assert repository.definition('second-leaf')['external_version'] == 'second-leaf'
            assert repository.learning_head()['groups'][0]['definition_filename']
            assert repository.lineage()[0]['evidence_filename']
            assert repository.successors('first-leaf')[0]['evidence_filename']
        assert not view.conn.in_transaction
        assert not any(statement.strip().upper().startswith('BEGIN') for statement in statements)

        read = group_documents.read

        def outside_transaction(*args, **kwargs):
            assert not view.conn.in_transaction
            return read(*args, **kwargs)

        monkeypatch.setattr(group_documents, 'read', outside_transaction)
        assert group_queries.service(view).head(members=True)['groups'][1]['version'] == 'second-leaf'
