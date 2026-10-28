"""Interrupted staging resumes committed prefixes without weakening publication guards."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import sqlite3

import pytest

from tests.group_support import publish, recordings
from tests.test_group_preparation import seed
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core.store import Store
from yt_playlist.rec import group_discovery, group_preparation, group_versions
from yt_playlist.repos import group_publication


@pytest.fixture
def database(tmp_path):
    store = Store(tmp_path / 'resume.db')
    store.init_schema()
    yield store
    store.conn.close()


def legacy(store):
    store.modes.replace_modes([{'mode_id': 1, 'label': 'Retained legacy mode', 'families': [],
        'centroid': [.2, .8], 'size': 12, 'rep_keys': [], 'space': 'original'}], [], 10)
    return group_versions.legacy_definition(store.modes.list_modes(False)[0])[0]


def pending(store, tracks, previous, now=2000):
    return group_versions.prepare(group_discovery.discover(tracks), tracks, previous,
        store.modes.list_modes(False), now=now, serial=store.taste_groups.serial(),
        input_fingerprint='same-input', policy=group_discovery.Policy(),
        legacy_versions=store.taste_groups.legacy_versions())


def interrupt(monkeypatch, *, writes=3, error=WorkBudgetExceeded):
    original = group_publication._write
    completed = 0

    @contextmanager
    def write(repo):
        nonlocal completed
        with original(repo) as conn:
            yield conn
        completed += 1
        if completed == writes:
            raise error('Controlled interruption after a committed batch')

    monkeypatch.setattr(group_publication, '_write', write)
    return original


def members(store, generation):
    return [tuple(row) for row in store.conn.execute('SELECT m.version,m.identity_key,m.similarity,m.support '
        'FROM rec_group_definitions d JOIN rec_group_members m USING(version) '
        'WHERE d.generation=? ORDER BY m.version,m.identity_key', (generation,))]


def finish(store, prepared, owner):
    store.conn.execute('BEGIN')
    with store.conn:
        store.taste_groups.finish_staged(prepared['generation'], owner)


@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('error', [WorkBudgetExceeded, WorkClosing])
def test_preparation_resumes_exact_plan_and_legacy_versions(database, monkeypatch, request, restart, error):
    tracks = recordings([('House', 12, .3)])
    seed(database, tracks)
    worker = group_preparation.Preparation(database)
    assert worker.step(1000)
    first = database.taste_groups.head(members=True)
    legacy_version = legacy(database)
    monkeypatch.setattr(group_publication, 'BATCH_ROWS', 4)
    original = interrupt(monkeypatch, error=error)
    worker.request_rebuild()
    if error is WorkClosing:
        with pytest.raises(WorkClosing):
            worker.step(2000)
    else:
        assert not worker.step(2000)
    snapshot, plan = worker._pending
    generation = plan['generation']
    prefix = members(database, generation)
    assert 0 < len(prefix) < sum(len(group['members']) for group in plan['groups'])
    assert database.taste_groups.head(members=True) == first
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        snapshot.store.conn.execute('SELECT 1')
    monkeypatch.setattr(group_publication, '_write', original)
    discovery = group_discovery.discover
    discovered = []

    def discover(*args, **kwargs):
        discovered.append(True)
        return discovery(*args, **kwargs)

    monkeypatch.setattr(group_discovery, 'discover', discover)
    if restart:
        # A replacement worker has neither the plan nor its owner token. Expiry
        # is a controlled fixture; the independent actual-library audit waits
        # the production interval and lease in real wall time.
        with database.conn:
            database.conn.execute('UPDATE rec_group_builds SET expires_at=0 WHERE generation=?', (generation,))
        path = database._db_path
        database.conn.close()
        database = Store(path)
        request.addfinalizer(database.conn.close)
        database.init_schema()
        worker = group_preparation.Preparation(database)
    assert worker.step(2060)
    assert len(discovered) == int(restart)
    assert worker._pending is None
    assert database.taste_groups.head_generation() == generation
    assert database.taste_groups.head()['created_at'] == 2000
    assert members(database, generation) == sorted((group['version'], *member)
        for group in plan['groups'] for member in group['members'])
    assert database.conn.execute('SELECT COUNT(*) FROM rec_group_graph WHERE generation=?',
                                 (generation,)).fetchone()[0] == len(plan['edges'])
    assert legacy_version in database.taste_groups.legacy_versions()
    assert database.taste_groups.definitions(first['generation'], members=True) == first['groups']
    assert not database.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_foreign_live_lease_cannot_be_adopted(database, monkeypatch):
    tracks = recordings([('House', 12, .3)])
    first = publish(database, tracks)
    plan = pending(database, tracks, first)
    monkeypatch.setattr(group_publication, 'BATCH_ROWS', 4)
    original = interrupt(monkeypatch)
    with pytest.raises(WorkBudgetExceeded):
        database.taste_groups.stage(plan)
    prefix = members(database, plan['generation'])
    owner = plan['_stage_owner']
    other = deepcopy(plan)
    other.pop('_stage_owner')
    other.pop('_stage_digest')
    monkeypatch.setattr(group_publication, '_write', original)
    with pytest.raises(WorkBudgetExceeded, match='leased by another worker'):
        database.taste_groups.stage(other)
    assert members(database, plan['generation']) == prefix
    assert database.conn.execute('SELECT owner FROM rec_group_builds WHERE generation=?',
                                 (plan['generation'],)).fetchone()[0] == owner
    finish(database, plan, database.taste_groups.stage(plan))
    assert database.taste_groups.head_generation() == plan['generation']


def test_cleanup_prefix_is_finished_before_rebuilding_same_generation(database, monkeypatch):
    tracks = recordings([('House', 12, .3)])
    first = publish(database, tracks)
    plan = pending(database, tracks, first)
    monkeypatch.setattr(group_publication, 'BATCH_ROWS', 4)
    original = interrupt(monkeypatch, writes=4)
    with pytest.raises(WorkBudgetExceeded):
        database.taste_groups.stage(plan)
    prefix = members(database, plan['generation'])
    assert len(prefix) == 8
    monkeypatch.setattr(group_publication, '_write', original)
    with database.conn:
        database.conn.execute('UPDATE rec_group_builds SET expires_at=0')
    assert database.taste_groups.cleanup_staged()
    assert len(members(database, plan['generation'])) == 4
    finish(database, plan, database.taste_groups.stage(plan))
    assert members(database, plan['generation']) == sorted((group['version'], *member)
        for group in plan['groups'] for member in group['members'])
    assert database.taste_groups.definitions(first['generation'], members=True) == first['groups']
    assert not database.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_different_output_cannot_reuse_staged_prefix(database, monkeypatch):
    tracks = recordings([('House', 12, .3)])
    first = publish(database, tracks)
    plan = pending(database, tracks, first)
    monkeypatch.setattr(group_publication, 'BATCH_ROWS', 4)
    original = interrupt(monkeypatch)
    with pytest.raises(WorkBudgetExceeded):
        database.taste_groups.stage(plan)
    prefix = members(database, plan['generation'])
    other = deepcopy(plan)
    other.pop('_stage_digest')
    other['edges'].append(('changed-left', 'changed-right', .8, .9))
    monkeypatch.setattr(group_publication, '_write', original)
    with pytest.raises(RuntimeError, match='output digest'):
        database.taste_groups.stage(other)
    assert members(database, plan['generation']) == prefix
    assert database.taste_groups.head(members=True) == first
    assert not database.conn.in_transaction


def test_unpublished_legacy_collision_waits_then_cleans_expired_owner(database, monkeypatch):
    tracks = recordings([('House', 12, .3)])
    first = publish(database, tracks)
    version = legacy(database)
    old = pending(database, tracks, first)
    new = pending(database, tracks, first, now=2100)
    monkeypatch.setattr(group_publication, 'BATCH_ROWS', 4)
    original = interrupt(monkeypatch)
    with pytest.raises(WorkBudgetExceeded):
        database.taste_groups.stage(old)
    monkeypatch.setattr(group_publication, '_write', original)
    with pytest.raises(WorkBudgetExceeded, match='still owned'):
        database.taste_groups.stage(new)
    assert database.conn.execute('SELECT generation FROM rec_group_definitions WHERE version=?',
                                 (version,)).fetchone()[0] == old['generation']
    assert database.taste_groups.head(members=True) == first
    with database.conn:
        database.conn.execute('UPDATE rec_group_builds SET expires_at=0 WHERE generation=?', (old['generation'],))
    finish(database, new, database.taste_groups.stage(new))
    assert database.conn.execute('SELECT generation FROM rec_group_definitions WHERE version=?',
                                 (version,)).fetchone()[0] == new['generation']
    assert not database.conn.execute('SELECT 1 FROM rec_group_generations WHERE generation=?',
                                     (old['generation'],)).fetchone()
    assert database.taste_groups.definitions(first['generation'], members=True) == first['groups']
    assert not database.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('change', ['input', 'unrelated-setting', 'external-writer'])
def test_cached_plan_revalidates_original_guard_between_turns(database, monkeypatch, change):
    tracks = recordings([('House', 12, .3)])
    seed(database, tracks)
    worker = group_preparation.Preparation(database)
    assert worker.step(1000)
    first = database.taste_groups.head(members=True)
    monkeypatch.setattr(group_publication, 'BATCH_ROWS', 4)
    original = interrupt(monkeypatch)
    worker.request_rebuild()
    assert not worker.step(2000)
    snapshot, plan = worker._pending
    probe = snapshot.publish
    results = []

    def publish_guard(*args, **kwargs):
        result = probe(*args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(snapshot, 'publish', publish_guard)
    monkeypatch.setattr(group_publication, '_write', original)
    if change == 'input':
        seed(database, [replace(tracks[0], values=(.95,) * len(tracks[0].values))])
    elif change == 'unrelated-setting':
        database.settings.set_setting('foreground-setting', 'new')
    else:
        with sqlite3.connect(database._db_path) as other:
            other.execute("INSERT INTO settings VALUES ('external-setting','new')")
    assert worker.step(2060)
    assert results == ([True, True] if change == 'unrelated-setting' else [False])
    if change == 'input':
        assert database.taste_groups.head_generation() != plan['generation']
    assert database.taste_groups.definitions(first['generation'], members=True) == first['groups']
    assert not database.conn.execute('PRAGMA foreign_key_check').fetchall()
