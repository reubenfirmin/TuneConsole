"""Last-good typed publications survive interrupted staging and failed cutovers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import sqlite3

import pytest

from tests.publication_support import plan, publish, stage
from yt_playlist.core import policy_config, recommendation_pools as pools
from yt_playlist.core.activity import WorkClosing, WorkBudgetExceeded
from yt_playlist.core.db_space import LowDiskSpace
from yt_playlist.core.migrations import current
from yt_playlist.core.migrations.runner import MigrationError
from yt_playlist.core.store import Store
from yt_playlist.repos import policy_storage, proposal_cache, recommendation_publication as publication


@pytest.fixture
def database(file_backed_store):
    return file_backed_store


@pytest.mark.parametrize('kind,name', [
    ('INDEX', 'ix_recommendation_build_expiry'),
    ('TRIGGER', 'frozen_recommendation_candidates_update'),
    ('TRIGGER', 'recommendation_generation_transition')])
def test_schema_validation_requires_publication_constraints(database, kind, name):
    database.conn.execute(f'DROP {kind} {name}')
    with pytest.raises(MigrationError, match=name):
        current.validate(database.conn)


def test_staging_releases_writer_between_bounded_commits_and_keeps_last_good_visible(database, monkeypatch):
    old = publish(database, plan(database, 'old'))
    prepared = plan(database, 'new', count=3 * publication.BATCH_ROWS + 7,
                    value={'title': 'café 🎵' * 1000})
    original, sizes = proposal_cache._write, []
    with ThreadPoolExecutor(1) as foreground:
        @contextmanager
        def observed(repo, **kwargs):
            with original(repo, **kwargs) as conn:
                before = conn.execute('SELECT count(*) FROM recommendation_candidates').fetchone()[0]
                yield conn
                sizes.append(conn.execute('SELECT count(*) FROM recommendation_candidates').fetchone()[0] - before)
            assert not repo._lock.held_by_current_thread and not repo.conn.in_transaction
            assert database.recommendations.head('home')['id'] == old.generation_id
            foreground.submit(database.settings.set_setting, 'foreground', 'saved').result(5)

        monkeypatch.setattr(proposal_cache, '_write', observed)
        build = stage(database, prepared)
    monkeypatch.setattr(proposal_cache, '_write', original)
    assert max(sizes) == publication.BATCH_ROWS
    assert sum(size > 0 for size in sizes) == 4
    database.recommendations.publish(build, 11)
    assert database.recommendations.output('home', 'fixture') == {'title': 'café 🎵' * 1000}


def test_interrupted_prefix_resumes_after_store_reopen_without_reinserting_rows(file_backed_store, monkeypatch):
    database = file_backed_store
    path = database._db_path
    prepared = plan(database, 'resumable', count=400)
    build = database.recommendations.claim(prepared)
    append = pools.append

    def interrupted(conn, identity, candidates, start):
        if start == publication.BATCH_ROWS * 2:
            raise WorkClosing('injected shutdown')
        return append(conn, identity, candidates, start)

    monkeypatch.setattr(pools, 'append', interrupted)
    with pytest.raises(WorkClosing, match='injected shutdown'):
        database.recommendations.stage(prepared, build)
    prefix = [tuple(row) for row in database.conn.execute('SELECT * FROM recommendation_candidates ORDER BY rank')]
    assert len(prefix) == publication.BATCH_ROWS * 2
    database.conn.close()
    database = Store(path)
    starts = []
    def resumed(conn, identity, candidates, start):
        starts.append(start)
        assert start >= len(prefix)
        return append(conn, identity, candidates, start)
    monkeypatch.setattr(pools, 'append', resumed)
    try:
        database.init_schema()
        reclaimed = database.recommendations.claim(prepared, owner=build.owner)
        assert reclaimed == build
        database.recommendations.stage(prepared, reclaimed)
        assert starts == [256, 384]
        assert [tuple(row) for row in database.conn.execute(
            'SELECT * FROM recommendation_candidates WHERE rank<256 ORDER BY rank')] == prefix
        database.recommendations.publish(reclaimed, 12)
        assert len(database.recommendations.page('home', 'all', 'wheelhouse', limit=512)) == 400
    finally:
        database.conn.close()


@pytest.mark.parametrize('outcome', ['success', 'stale', 'failure'])
def test_policy_then_heads_commit_before_the_settings_acknowledgement(database, outcome):
    old = publish(database, plan(database, 'old'))
    build = stage(database, plan(database, 'new'))
    database.settings.set_setting('input', 'old')
    row, base = policy_config.pack({'ranker': 'ordered-side-policy'}, 10)
    with database.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting('input') == 'old'
        assert snapshot.store.recommendations.head('home')['id'] == old.generation_id
        snapshot.release_reader()
        if outcome == 'stale':
            database.settings.set_setting('input', 'changed')
        def finish():
            database.rec_ledger.publish_prepared_policies([row], [base])
            database.recommendations.publish(build, 12)
            if outcome == 'failure':
                raise ValueError('injected cutover failure')
            database.settings.publish_values({'published': 'new'})
        if outcome == 'failure':
            with pytest.raises(ValueError, match='injected cutover'):
                snapshot.publish_ordered(finish)
        else:
            assert snapshot.publish_ordered(finish) is (outcome == 'success')
    succeeded = outcome == 'success'
    for surface in ('home', 'fresh'):
        assert database.recommendations.head(surface)['id'] == (old if outcome == 'stale' else build).generation_id
    assert database.settings.get_setting('published') == ('new' if succeeded else None)
    assert database.rec_ledger.policy_configuration(row[0]) == (
        None if outcome == 'stale' else {'ranker': 'ordered-side-policy'})
    assert not database.conn.in_transaction


def test_changed_head_rejects_old_snapshot_but_private_staging_does_not(database):
    old = publish(database, plan(database, 'old'))
    prepared = plan(database, 'new', value={'title': 'old'})
    with database.read_snapshot() as snapshot:
        # Guard the published head alone. Reading full generation rows also
        # conservatively guards private generation staging on the current schema.
        assert snapshot.store.conn.execute("SELECT generation_id FROM recommendation_heads WHERE surface='home'").fetchone()[0] == old.generation_id
        snapshot.release_reader()
        build = stage(database, prepared)
        assert snapshot.publish(lambda: database.settings.publish_values({'same': 'accepted'}))
    with database.read_snapshot() as snapshot:
        snapshot.store.conn.execute("SELECT generation_id FROM recommendation_heads WHERE surface='home'").fetchone()
        snapshot.release_reader()
        database.recommendations.publish(build, 12)
        assert not snapshot.publish(lambda: database.settings.publish_values({'stale': 'forbidden'}))
    assert database.settings.get_setting('same') == 'accepted'
    assert database.settings.get_setting('stale') is None


def test_cleanup_is_bounded_keeps_active_generation_and_preserves_pinned_reader(database):
    old = publish(database, plan(database, 'old', count=1100))
    new_plan = plan(database, 'new', count=1)
    # A reporting reader can observe later commits. This contract explicitly
    # owns the pinned generation while its old rows and files are retired.
    with database.read_snapshot() as snapshot:
        reader = snapshot.store
        assert reader.recommendations.head('home')['id'] == old.generation_id
        new = publish(database, new_plan)
        for _ in range(20):
            retired = publication.cleanup_generations(database.recommendations)
            assert retired.rows <= publication.CLEANUP_ROWS
            retired_pools = publication.cleanup_pools(database.recommendations)
            assert retired_pools.rows <= publication.CLEANUP_ROWS
            if not retired and not retired_pools:
                break
        else:
            pytest.fail('Bounded cleanup did not finish')
        assert len(reader.recommendations.page('home', 'all', 'wheelhouse', after=1023)) == 76
        assert reader.recommendations.output('home', 'fixture') == {'title': 'old'}
    assert database.recommendations.head('home')['id'] == new.generation_id
    assert database.recommendations.output('home', 'fixture') == {'title': 'new'}
    assert not database.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_low_disk_space_during_staging_keeps_last_good_publication(database, monkeypatch):
    old = publish(database, plan(database, 'old'))
    prepared = plan(database, 'new', count=400)
    append = pools.append
    def full(conn, identity, candidates, start):
        if start >= publication.BATCH_ROWS:
            raise LowDiskSpace('injected full disk')
        return append(conn, identity, candidates, start)
    monkeypatch.setattr(pools, 'append', full)
    with pytest.raises(LowDiskSpace, match='injected full disk'):
        stage(database, prepared)
    assert database.recommendations.head('home')['id'] == old.generation_id
    assert not database.conn.in_transaction


def test_incomplete_build_cannot_publish_skip_a_rank_or_become_ready(database):
    prepared = plan(database, 'incomplete', count=400)
    build = database.recommendations.claim(prepared)
    with database.conn:
        database.conn.execute('BEGIN IMMEDIATE')
        identity = publication._create_pool(database.conn, prepared.slots[0].pool)
        database.conn.execute('INSERT INTO recommendation_build_pools VALUES (?,?)', (build.generation_id, identity))
        pools.append(database.conn, identity, prepared.slots[0].pool.candidates[:128], 0)
    with pytest.raises(ValueError, match='unfinished'):
        database.recommendations.publish(build, 12)
    with database.conn:
        database.conn.execute('BEGIN IMMEDIATE')
        pools.append(database.conn, identity, prepared.slots[0].pool.candidates[256:257], 256)
    # Private rows may arrive out of order; a gap must never be sealed or served.
    with pytest.raises(ValueError, match='incomplete membership'):
        pools.verify(database.conn, identity, prepared.slots[0].pool)
    with pytest.raises(sqlite3.IntegrityError), database.conn:
        database.conn.execute('UPDATE recommendation_pools SET complete=1 WHERE id=?', (identity,))
    with pytest.raises(sqlite3.IntegrityError), database.conn:
        database.conn.execute("UPDATE recommendation_generations SET status='ready' WHERE id=?", (build.generation_id,))
    assert database.recommendations.head('home') is None


def test_concurrent_identical_stagers_require_the_same_lease_owner(database):
    prepared = plan(database, 'concurrent')
    with ThreadPoolExecutor(2) as workers:
        attempts = [workers.submit(database.recommendations.claim, prepared) for _ in range(2)]
        builds, failures = [], []
        for attempt in attempts:
            try:
                builds.append(attempt.result(15))
            except WorkBudgetExceeded as exc:
                failures.append(str(exc))
    assert len(builds) == len(failures) == 1 and 'leased' in failures[0]
    assert database.recommendations.claim(prepared, owner=builds[0].owner) == builds[0]
    database.recommendations.stage(prepared, builds[0])
    assert database.conn.execute('SELECT count(*) FROM recommendation_generations').fetchone()[0] == 1
    assert database.recommendations.head('home') is None


def test_expired_cleanup_restarts_with_a_new_generation_without_partial_exposure(database):
    prepared = plan(database, 'expired', count=400)
    build = stage(database, prepared)
    with database.conn:
        database.conn.execute('UPDATE recommendation_builds SET expires_at=0 WHERE generation_id=?', (build.generation_id,))
    assert database.recommendations.cleanup()
    while database.recommendations.cleanup():
        pass
    assert database.recommendations.head('home') is None
    replacement = publish(database, replace(prepared, external_key='replacement'))
    assert database.recommendations.head('home')['id'] == replacement.generation_id
    assert len(database.recommendations.page('home', 'all', 'wheelhouse', limit=512)) == 400


@pytest.mark.parametrize('operation', ['put', 'update', 'delete'])
def test_legacy_replacement_and_raw_invalidation_cannot_replace_the_domain_owner(database, operation):
    old = publish(database, plan(database, 'old'))
    if operation == 'put':
        with pytest.raises(sqlite3.IntegrityError, match='domain owner'):
            database.rec_proposals.put_proposals('mode_bundles', {'replacement': True}, 3)
    else:
        # Retired rows are absent, so a zero-row raw operation cannot replace a head.
        with database.conn:
            database.conn.execute("UPDATE rec_proposals SET payload='{}' WHERE surface='mode_bundles'" if operation == 'update'
                                  else "DELETE FROM rec_proposals WHERE surface='mode_bundles'")
    assert database.recommendations.head('home')['id'] == old.generation_id


def test_database_rejects_mutating_or_cleaning_published_state(database):
    publish(database, plan(database, 'published'))
    for sql in ('UPDATE recommendation_candidates SET score=score', 'DELETE FROM recommendation_candidates',
                "UPDATE recommendation_generations SET status='building'", 'DELETE FROM prepared_outputs'):
        with pytest.raises(sqlite3.IntegrityError), database.conn:
            database.conn.execute(sql)
    assert database.recommendations.output('home', 'fixture') == {'title': 'published'}


@pytest.mark.parametrize('value,expected', [(None, False), ({}, False), ([], True), ({'all': {}}, True)])
def test_presence_matches_output_contract_without_reading_files(database, monkeypatch, value, expected):
    database.rec_proposals.put_proposals('trend_rollups', value, 1)
    from yt_playlist.core import recommendation_outputs
    monkeypatch.setattr(recommendation_outputs, 'read', lambda *a, **k: pytest.fail('Presence loaded output bytes'))
    assert database.rec_proposals.has_proposals('trend_rollups') is expected


def test_policy_retention_protects_active_and_unfinished_publication_owners(database):
    database.rec_ledger.record_serving_policy('old', {'ranker': 'old'}, 1)
    publish(database, plan(database, 'active', policy='active'))
    stage(database, plan(database, 'unfinished', policy='unfinished'))
    retained = policy_storage.retained_closure(database.conn, 10000)['recommendation_policies']
    assert retained & {'old', 'active', 'unfinished'} == {'active', 'unfinished'}
    database.rec_ledger._prune_policy_batch(10000)
    assert database.rec_ledger.policy_configuration('old') is None
    assert database.rec_ledger.policy_configuration('active') == {'ranker': 'active'}
    assert database.rec_ledger.policy_configuration('unfinished') == {'ranker': 'unfinished'}


def test_shared_pool_projection_does_not_inflate_mode_copies(database, monkeypatch):
    prepared = plan(database, 'shared', count=100)
    prepared = replace(prepared, slots=(*prepared.slots,
        publication.Slot('home', 'smaller-genre', 'wheelhouse', prepared.slots[0].pool)))
    publish(database, prepared)
    from yt_playlist.core import json_cache
    monkeypatch.setattr(json_cache, 'unpack', lambda *a: pytest.fail('Typed projection decoded a global bundle'))
    assert database.conn.execute('SELECT count(*) FROM recommendation_candidates').fetchone()[0] == 100
    assert database.recommendations.page('home', 'all', 'wheelhouse') == database.recommendations.page('home', 'smaller-genre', 'wheelhouse')
