"""Exercise leased compact builds, interruption and shared-content ownership."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
from threading import Event

import pytest

from tests.core.test_compact_groups_migration import predecessor, apply
from yt_playlist.core.activity import WorkClosing
from yt_playlist.core import group_documents
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.migrations import v072_compact_groups as migration
from yt_playlist.core.store import Store
from yt_playlist.repos import compact_group_publication as compact, group_publication as publication, group_queries
from yt_playlist.repos.compact_taste_groups import CompactTasteGroupsRepo


@pytest.fixture
def database(predecessor):
    conn = predecessor
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
    apply(conn)
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    store = Store(path)
    store._bind_repositories()
    store.conn.execute('PRAGMA journal_mode=WAL')
    store._snapshot_revisions = Revisions(store.conn)
    yield store, prepared
    store.conn.close()


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
    owner = compact.stage(store.taste_groups, prepared)
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
        compact.stage(store.taste_groups, prepared)
    assert group_queries.head_generation(store.conn) == 'second'
    assert group_queries.definition(store.conn, 'third-leaf') is None
    assert store.conn.execute('SELECT count(*) FROM group_build_versions').fetchone()[0] == 1
    with pytest.raises(RuntimeError, match='incomplete or no longer owned'):
        finish(store, prepared, prepared['_stage_owner'])
    monkeypatch.setattr(publication, '_write', original)
    owner = compact.stage(store.taste_groups, prepared)
    finish(store, prepared, owner)
    assert store.conn.execute('SELECT count(*) FROM group_versions').fetchone()[0] == 3
    assert store.conn.execute('SELECT count(*) FROM group_members').fetchone()[0] == 4
    assert group_queries.head(store.conn, members=True)['groups'][1]['members'] == {'one': (.5, .8)}


def test_live_lease_blocks_takeover_and_expired_lease_fences_the_old_owner(database, monkeypatch):
    store, prepared = database
    prepared = changed(prepared)
    original = interrupt_after_member_batch(monkeypatch)
    with pytest.raises(WorkClosing):
        compact.stage(store.taste_groups, prepared)
    monkeypatch.setattr(publication, '_write', original)
    other = deepcopy(prepared)
    old_owner = other.pop('_stage_owner')
    with pytest.raises(publication.BuildUnavailable, match='leased by another worker'):
        compact.stage(store.taste_groups, other)
    with store.conn:
        store.conn.execute('UPDATE group_builds SET expires_at=0')
    new_owner = compact.stage(store.taste_groups, other)
    assert new_owner != old_owner
    with pytest.raises(RuntimeError, match='incomplete or no longer owned'):
        finish(store, prepared, old_owner)
    finish(store, other, new_owner)
    assert group_queries.head_generation(store.conn) == 'third'


def test_expired_cleanup_removes_only_the_abandoned_build_and_can_rebuild(database, monkeypatch):
    store, prepared = database
    prepared = changed(prepared)
    original = interrupt_after_member_batch(monkeypatch)
    with pytest.raises(WorkClosing):
        compact.stage(store.taste_groups, prepared)
    monkeypatch.setattr(publication, '_write', original)
    assert not compact.cleanup(store.taste_groups)
    with store.conn:
        store.conn.execute('UPDATE group_builds SET expires_at=0')
    monkeypatch.setattr(publication, 'BATCH_ROWS', 1)
    for _ in range(100):
        if not compact.cleanup(store.taste_groups):
            break
        assert group_queries.head_generation(store.conn) == 'second'
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    else:
        pytest.fail('Compact cleanup did not finish its bounded deletions')
    assert store.conn.execute('SELECT count(*) FROM group_versions').fetchone()[0] == 2
    assert store.conn.execute('SELECT count(*) FROM group_members').fetchone()[0] == 3
    assert not store.conn.execute("SELECT 1 FROM group_generations WHERE external_key='third'").fetchone()
    owner = compact.stage(store.taste_groups, prepared)
    finish(store, prepared, owner)
    assert group_queries.head_generation(store.conn) == 'third'


@pytest.mark.parametrize('interrupt_cleanup', [False, True])
def test_new_plan_clears_expired_foreign_aliases_with_bounded_restartable_cleanup(database, monkeypatch,
                                                                                interrupt_cleanup):
    store, prepared = database
    abandoned_owner = compact.stage(store.taste_groups, prepared)
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
            compact.stage(store.taste_groups, next_plan)
        assert not store.conn.execute("SELECT 1 FROM group_generations WHERE external_key='fourth'").fetchone()
    owner = compact.stage(store.taste_groups, next_plan)
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
    owner = compact.stage(store.taste_groups, prepared)
    next_plan = deepcopy(prepared)
    next_plan.update(generation='fourth', at=4, serial=4)
    next_plan.pop('_stage_owner')
    before = list(store.conn.iterdump())
    with pytest.raises(publication.BuildUnavailable, match='leased by another unpublished build'):
        compact.stage(store.taste_groups, next_plan)
    assert list(store.conn.iterdump()) == before
    finish(store, prepared, owner)
    assert group_queries.head_generation(store.conn) == 'third'


@pytest.mark.parametrize('change', ['none', 'recording', 'external', 'head'])
def test_hidden_append_preserves_the_original_publication_guard(database, change):
    store, prepared = database
    with store.read_snapshot() as snapshot:
        assert group_queries.head(snapshot.store.conn, members=True)['generation'] == 'second'
        snapshot.release_reader()
        owner = compact.stage(store.taste_groups, prepared)
        if change == 'recording':
            # The outer preparation declares the exact input identities too.
            snapshot.store._snapshot_reads.include({('recordings', '')})
            with store.conn:
                store.conn.execute("UPDATE recordings SET title='changed input' WHERE identity_key='one'")
        elif change == 'external':
            import sqlite3
            with sqlite3.connect(store._db_path) as external:
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
        compact.stage(store.taste_groups, prepared)
    monkeypatch.setattr(publication, '_write', original)
    altered = deepcopy(prepared)
    altered.pop('_stage_digest')
    value = json.loads(altered['groups'][1]['payload'])
    value['name'] = 'Changed output under the same generation identity'
    altered['groups'][1]['payload'] = json.dumps(value)
    before = list(store.conn.iterdump())
    with pytest.raises(RuntimeError, match='retained output digest'):
        compact.stage(store.taste_groups, altered)
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
        work = pool.submit(compact.stage, store.taste_groups, prepared)
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


def test_store_retains_group_files_until_the_last_old_snapshot_read(database, tmp_path):
    store, _prepared = database
    with store.read_view() as view:
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


def test_compact_repository_atomic_import_and_assignment_owner_checks(database):
    store, prepared = database
    repo = CompactTasteGroupsRepo(store)
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        repo.publish(prepared)
        repo.publish_assignments([
            ('two', 'third', 'third-leaf', .7, .6, 'known-profile', 4),
            ('unknown-new-key', 'third', None, 0, 0, None, 4)])
    assert repo.head_generation() == 'third'
    assert repo.head_checkpoint()['generation'] == 'third'
    assert repo.head(members=True)['groups'][1]['members'] == {'one': (.9, .8)}
    assert repo.assignment_head()['groups'] == [{'version': 'third-leaf', 'rep_keys': ['one']}]
    assert repo.assignment('two') == {'identity_key': 'two', 'generation': 'third', 'version': 'third-leaf',
        'similarity': .7, 'support': .6, 'profile_digest': 'known-profile', 'prepared_at': 4}
    assert repo.assignment('unknown-new-key')['version'] is None
    assert store.conn.execute("SELECT resolution_reason FROM unresolved_group_assignments "
                               "WHERE raw_key='unknown-new-key'").fetchone()[0] == 'missing_recording'
    with pytest.raises(ValueError, match='invalid generation or selected version'), store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        repo.publish_assignments([('two', 'third', 'second-leaf', .7, .6, 'wrong-alias', 5)])
    assert repo.assignment('two')['profile_digest'] == 'known-profile'
    assert not repo.expired_builds()
    assert repo.resumable_build('input-fingerprint', 3, prepared['policy'], 'second') is None


def test_real_offer_learning_reuses_the_same_external_alias_in_two_generations(database):
    from tests.test_group_learning import menu, compiled
    from yt_playlist.rec import choice_evidence as choices
    store, prepared = database
    # Only bind this controlled post-v72 test. Production stays on schema 69.
    store.taste_groups = CompactTasteGroupsRepo(store)
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
