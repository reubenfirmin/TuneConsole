"""Real SQL for bounded ranks, private leases, publication and shared lifetime."""
from dataclasses import replace
import sqlite3

import pytest

from tests.core.test_compact_groups_migration import predecessor
from tests.repos.test_compact_group_publication import database
from tests.repos.test_group_retention import drain, enable
from yt_playlist.core import recommendation_outputs as outputs, recommendation_pools as pools
from yt_playlist.core.activity import WorkClosing, WorkBudgetExceeded
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.migrations import v073_recommendation_publications as migration
from yt_playlist.core.migrations.runner import Context
from yt_playlist.repos import group_retention, recommendation_publication as publication


@pytest.fixture
def recommendations(database):
    store, _ = database
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        migration.apply(store.conn, Context(4))
    migration.validate(store.conn)
    # This controlled v73 fixture applies individual boundaries without a
    # contiguous admission history. Hydration still needs the same historical
    # name projections as supported predecessor startup, including read views.
    store._schema_compatibility = True
    store._bind_repositories()
    store._snapshot_revisions = Revisions(store.conn)
    return store


def plan(store, key='one', *, candidates=None, fingerprint='a' * 64, group=1):
    policy = store.conn.execute('SELECT policy_version FROM group_generations WHERE id=1').fetchone()[0]
    pool = pools.prepare('wheelhouse', policy, None, fingerprint, 'card',
                         candidates if candidates is not None else [pools.Candidate(1, .9), pools.Candidate(2, .7)])
    return publication.Plan(key, policy, group, 1, 10,
        (publication.Slot('home', 'all', 'wheelhouse', pool), publication.Slot('home', 'small-genre', 'wheelhouse', pool)),
        (publication.Output('home', 'framing', outputs.encode('home', 'framing', {'title': key})),))


def publish(store, prepared):
    build = store.recommendations.claim(prepared)
    store.recommendations.stage(prepared, build)
    store.recommendations.publish(build, 11)
    return build


def collect(store):
    for _ in range(200):
        if not store.recommendations.cleanup():
            break
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    else:
        pytest.fail('Bounded recommendation cleanup did not finish')


def test_partial_rows_are_hidden_until_one_atomic_head_switch(recommendations):
    store = recommendations
    prepared = plan(store)
    prepared = replace(prepared, slots=prepared.slots + tuple(
        replace(prepared.slots[0], mode_key=f'genre-{i}') for i in range(18)))
    build = store.recommendations.claim(prepared)
    assert store.recommendations.head('home') is None
    assert store.recommendations.page('home', 'all', 'wheelhouse') == []
    store.recommendations.stage(prepared, build)
    assert store.recommendations.head('home') is None
    assert store.conn.execute('SELECT status FROM recommendation_generations').fetchone()[0] == 'ready'
    # Retry crosses the bounded selection page without duplicating sealed pools.
    store.recommendations.stage(prepared, build)
    with pytest.raises(RuntimeError, match='idle connection'), store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        store.recommendations.publish(build, 11)
    store.recommendations.publish(build, 11)
    assert store.recommendations.head('home')['id'] == build.generation_id
    assert [row['key'] for row in store.recommendations.page('home', 'small-genre', 'wheelhouse')] == ['one', 'two']
    assert [row['key'] for row in store.recommendations.page('home', 'genre-17', 'wheelhouse')] == ['one', 'two']
    assert len(store.recommendations.selections('home', 'wheelhouse')) == 20
    assert store.recommendations.output('home', 'framing') == {'title': 'one'}
    assert not store.conn.execute('SELECT 1 FROM recommendation_builds').fetchone()
    assert store.conn.execute('SELECT count(*) FROM recommendation_pools').fetchone()[0] == 1
    assert store.conn.execute('SELECT count(*) FROM recommendation_candidates').fetchone()[0] == 2


def test_queried_bundle_framing_is_typed_and_frozen_with_its_generation(recommendations):
    store = recommendations
    framing = publication.Framing(presence=2047, comfort_pool=17, year_low=1987, year_high=2004,
        full_pools=True, unmodeled=False, musical_generation='musical-input-sha', radio_generation='model-7',
        radio_space='embedding-space', intensity_version=1, radio_profile_version='profile-2',
        vibe_version='vibes-5', vibe_profile_version='profile-2')
    prepared = replace(plan(store), framing=framing)
    build = publish(store, prepared)
    actual = store.recommendations.framing('home')
    assert actual['generation_id'] == build.generation_id
    assert (actual['year_low'], actual['year_high'], actual['comfort_pool'], actual['radio_space']) == (1987, 2004, 17, 'embedding-space')
    for sql in ('UPDATE recommendation_bundle_framing SET comfort_pool=0', 'DELETE FROM recommendation_bundle_framing'):
        with pytest.raises(sqlite3.IntegrityError), store.conn:
            store.conn.execute(sql)
    with pytest.raises(ValueError, match='framing'):
        store.recommendations.claim(replace(prepared, external_key='bad-year-bands',
                                            framing=replace(framing, year_high=1900)))


@pytest.mark.parametrize('interruption', ['closing', 'superseded'])
def test_interrupted_members_resume_the_exact_committed_prefix(recommendations, monkeypatch, interruption):
    store = recommendations
    with store.conn:
        store.conn.executemany('INSERT INTO recordings(id,identity_key) VALUES (?,?)',
                               [(i, f'recording-{i}') for i in range(3, 303)])
    prepared = plan(store, candidates=[pools.Candidate(i, 1 / i) for i in range(1, 303)])
    build = store.recommendations.claim(prepared)
    original, interrupted = publication.pools.append, False

    def append(conn, pool, candidates, start):
        nonlocal interrupted
        if start and not interrupted:
            interrupted = True
            raise WorkClosing('controlled interrupted publication')
        result = original(conn, pool, candidates, start)
        if interruption == 'superseded':
            interrupted = True
        return result
    monkeypatch.setattr(publication.pools, 'append', append)
    if interruption == 'closing':
        with pytest.raises(WorkClosing):
            store.recommendations.stage(prepared, build)
    else:
        assert store.recommendations.stage(prepared, build, still_current=lambda: not interrupted) is False
    assert store.conn.execute('SELECT count(*) FROM recommendation_candidates').fetchone()[0] == publication.BATCH_ROWS
    assert store.recommendations.head('home') is None
    resumed = store.recommendations.claim(prepared, owner=build.owner)
    assert resumed == build
    store.recommendations.stage(prepared, resumed)
    store.recommendations.publish(resumed, 12)
    after, identities = -1, []
    while page := store.recommendations.page('home', 'all', 'wheelhouse', after=after, limit=31):
        assert len(page) <= 31
        identities.extend(row['recording_id'] for row in page)
        after = page[-1]['rank']
    assert identities == list(range(1, 303))


@pytest.mark.parametrize('interruption', ['closing', 'superseded'])
def test_interrupted_slot_batch_resumes_all_remaining_shared_selections(recommendations, monkeypatch, interruption):
    store = recommendations
    first = plan(store)
    slots = tuple(publication.Slot('home', f'style-{i}', 'wheelhouse', first.slots[0].pool)
                  for i in range(publication.SLOT_BATCH_ROWS + 2))
    prepared = replace(first, slots=slots)
    build = store.recommendations.claim(prepared)
    original = publication._pool
    calls, changed = 0, False

    def interrupted(repo, current, pool, **kwargs):
        nonlocal calls, changed
        calls += 1
        if interruption == 'closing' and calls == publication.SLOT_BATCH_ROWS + 1:
            raise WorkClosing('controlled interruption after the first slot batch')
        result = original(repo, current, pool, **kwargs)
        if interruption == 'superseded' and calls == publication.SLOT_BATCH_ROWS:
            changed = True
        return result

    with monkeypatch.context() as boundary:
        boundary.setattr(publication, '_pool', interrupted)
        if interruption == 'closing':
            with pytest.raises(WorkClosing):
                store.recommendations.stage(prepared, build)
        else:
            assert store.recommendations.stage(prepared, build, still_current=lambda: not changed) is False
    assert store.recommendations.head('home') is None
    assert store.conn.execute('SELECT count(*) FROM recommendation_surface_pools').fetchone()[0] == publication.SLOT_BATCH_ROWS
    resumed = store.recommendations.claim(prepared, owner=build.owner)
    assert resumed == build
    store.recommendations.stage(prepared, resumed)
    store.recommendations.publish(resumed, 12)
    assert {row['mode_key'] for row in store.recommendations.selections('home', 'wheelhouse')} == {
        slot.mode_key for slot in slots}
    assert store.conn.execute('SELECT count(*) FROM recommendation_pools').fetchone()[0] == 1
    assert store.conn.execute('SELECT count(*) FROM recommendation_candidates').fetchone()[0] == 2
    for slot in slots:
        assert [row['key'] for row in store.recommendations.page('home', slot.mode_key, 'wheelhouse')] == ['one', 'two']
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_changed_pool_does_not_replace_unchanged_ranking_facts(recommendations):
    store = recommendations
    first = plan(store)
    publish(store, first)
    unchanged = store.recommendations.pool('home', 'all', 'wheelhouse')['id']
    original = [tuple(row) for row in store.conn.execute('SELECT * FROM recommendation_candidates ORDER BY rank')]
    changed = pools.prepare('explore', first.policy_version, None, 'b' * 64, 'card', [pools.Candidate(2, .8)])
    second = replace(first, external_key='two', input_revision=2, slots=first.slots +
                     (publication.Slot('home', 'all', 'explore', changed),))
    publish(store, second)
    collect(store)
    assert store.recommendations.pool('home', 'all', 'wheelhouse')['id'] == unchanged
    assert [tuple(row) for row in store.conn.execute('SELECT * FROM recommendation_candidates WHERE pool_id=? ORDER BY rank',
                                                   (unchanged,))] == original
    assert store.conn.execute('SELECT count(*) FROM recommendation_pools').fetchone()[0] == 2
    assert store.conn.execute('SELECT count(*) FROM recommendation_generations').fetchone()[0] == 1


def test_live_owner_fences_takeover_and_expired_takeover_fences_stale_completion(recommendations):
    store = recommendations
    prepared = plan(store)
    original = store.recommendations.claim(prepared)
    with pytest.raises(WorkBudgetExceeded, match='another worker'):
        store.recommendations.claim(prepared)
    with store.conn:
        store.conn.execute('UPDATE recommendation_builds SET expires_at=0')
    replacement = store.recommendations.claim(prepared)
    assert replacement.owner != original.owner
    store.recommendations.stage(prepared, replacement)
    with pytest.raises(WorkBudgetExceeded):
        store.recommendations.publish(original, 12)
    assert store.recommendations.head('home') is None
    store.recommendations.publish(replacement, 12)


def test_late_snapshot_conflict_and_late_sql_failure_leave_last_good_head(recommendations):
    store = recommendations
    previous = publish(store, plan(store))
    store.settings.set_setting('schema-publication-guard', 'before')
    with store.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting('schema-publication-guard') == 'before'
        second = plan(store, 'two')
        snapshot.release_reader()
        build = store.recommendations.claim(second)
        store.recommendations.stage(second, build)
        store.settings.set_setting('schema-publication-guard', 'after')
        assert not snapshot.publish_ordered(lambda: store.recommendations.publish(build, 12))
    assert store.recommendations.head('home')['id'] == previous.generation_id
    with store.conn:
        store.conn.execute("CREATE TEMP TRIGGER controlled_head_failure BEFORE INSERT ON recommendation_heads "
                           "BEGIN SELECT RAISE(ABORT,'controlled head failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled head failure'):
        store.recommendations.publish(build, 12)
    assert store.recommendations.head('home')['id'] == previous.generation_id
    assert store.conn.execute('SELECT owner FROM recommendation_builds WHERE generation_id=?',
                              (build.generation_id,)).fetchone()[0] == build.owner
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_head_failure')
    store.recommendations.publish(build, 12)
    assert store.recommendations.head('home')['id'] == build.generation_id


def test_changed_inputs_during_output_files_stop_staging_and_keep_last_good_head(recommendations, monkeypatch):
    store = recommendations
    previous = publish(store, plan(store))
    store.settings.set_setting('output-publication-guard', 'before')
    documents = tuple(publication.Output('home', f'part-{i}',
        outputs.encode('home', f'part-{i}', {'part': i}))
        for i in range(publication.OUTPUT_BATCH_ROWS * 2 + 2))
    prepared = replace(plan(store, 'obsolete-output-build'), outputs=documents)
    original_publish, changed = outputs.publish, []

    def publish_file(root, encoded):
        result = original_publish(root, encoded)
        if not changed:
            # A real strict input changes while durable files are still private.
            # This runs outside the manifest writer transaction.
            store.settings.set_setting('output-publication-guard', 'after')
            changed.append(True)
        return result

    with store.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting('output-publication-guard') == 'before'
        snapshot.release_reader()
        build = store.recommendations.claim(prepared)
        with monkeypatch.context() as boundary:
            boundary.setattr(outputs, 'publish', publish_file)
            assert store.recommendations.stage(
                prepared, build, still_current=snapshot.is_current) is False
        assert changed == [True]
        staged = store.conn.execute(
            'SELECT chunk_key FROM prepared_outputs WHERE generation_id=?', (build.generation_id,)).fetchall()
        assert 0 < len(staged) < len(documents), 'Obsolete work must stop before staging every output'
        assert not snapshot.publish_ordered(lambda: store.recommendations.publish(build, 12))
    assert store.recommendations.head('home')['id'] == previous.generation_id
    assert store.recommendations.output('home', 'framing') == {'title': 'one'}
    assert store.recommendations.output('home', 'part-0') is None

    # A new build can still publish every output after the changed input settles.
    with store.read_snapshot() as current:
        assert current.store.settings.get_setting('output-publication-guard') == 'after'
        current.release_reader()
        renewed = replace(prepared, external_key='current-output-build')
        ready = store.recommendations.claim(renewed)
        assert store.recommendations.stage(renewed, ready, still_current=current.is_current)
        assert current.publish_ordered(lambda: store.recommendations.publish(ready, 13))
    assert store.recommendations.head('home')['id'] == ready.generation_id
    for i in range(len(documents)):
        assert store.recommendations.output('home', f'part-{i}') == {'part': i}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_published_rows_cannot_mutate_and_seal_requires_exact_complete_membership(recommendations):
    store = recommendations
    build = publish(store, plan(store))
    for sql in ("UPDATE recommendation_generations SET status='ready'", 'UPDATE recommendation_candidates SET score=0',
                'DELETE FROM recommendation_candidates', 'UPDATE recommendation_surface_pools SET pool_id=999',
                'DELETE FROM prepared_outputs', 'UPDATE prepared_outputs SET chunk_key=\'different\'',
                'DELETE FROM recommendation_generations'):
        with pytest.raises(sqlite3.IntegrityError), store.conn:
            store.conn.execute(sql)
    assert store.recommendations.head('home')['id'] == build.generation_id
    second = store.recommendations.claim(plan(store, 'two'))
    with pytest.raises(sqlite3.IntegrityError, match='incomplete'), store.conn:
        store.conn.execute("UPDATE recommendation_generations SET status='ready' WHERE id=?", (second.generation_id,))


@pytest.mark.parametrize('kind', ['offer', 'response', 'comparison', 'prepared', 'pending', 'checkpoint'])
def test_actual_generation_references_keep_history_until_their_explicit_retirement(recommendations, kind):
    store = recommendations
    first = publish(store, plan(store))
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        store.recommendations.retain(kind, 'actual-evidence', first.generation_id)
    publish(store, plan(store, 'two'))
    collect(store)
    assert store.recommendations.output('home', 'framing', generation=first.generation_id) == {'title': 'one'}
    assert store.conn.execute('SELECT count(*) FROM recommendation_generations').fetchone()[0] == 2
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        store.recommendations.release(kind, 'actual-evidence')
    collect(store)
    assert store.conn.execute('SELECT count(*) FROM recommendation_generations').fetchone()[0] == 1
    assert store.conn.execute('SELECT count(*) FROM recommendation_candidates').fetchone()[0] == 2


def test_radio_membership_and_typed_filters_have_no_global_head_cutoff(recommendations):
    store = recommendations
    with store.conn:
        store.conn.executemany('INSERT INTO recordings(id,identity_key) VALUES (?,?)',
                               [(i, f'recording-{i}') for i in range(3, 1003)])
    prepared = plan(store)
    radio = pools.prepare('style-radio', prepared.policy_version, None, 'c' * 64, 'radio',
        [pools.Candidate(i, None, 12 if i > 950 else 40, 'rare' if i > 950 else 'common') for i in range(1, 1003)])
    publish(store, replace(prepared, slots=(publication.Slot('radio', 'style', 'membership', radio),), outputs=()))
    assert store.recommendations.pool('radio', 'style', 'membership')['eligible_count'] == 1002
    rows = store.recommendations.page('radio', 'style', 'membership', limit=17, band=12, genres=['rare'])
    assert len(rows) == 17 and rows[0]['recording_id'] == 951
    second = store.recommendations.page('radio', 'style', 'membership', after=rows[-1]['rank'], limit=17,
                                         band=12, genres=['rare'])
    assert second[0]['recording_id'] == 968
    assert set(row['recording_id'] for row in rows).isdisjoint(row['recording_id'] for row in second)


def test_missing_historical_recordings_keep_exact_raw_rank_without_inventing_an_owner(recommendations):
    store = recommendations
    prepared = plan(store, candidates=[pools.Candidate(1, .9), pools.UnresolvedCandidate('removed-old-key', .8),
                                        pools.Candidate(2, .7)])
    publish(store, prepared)
    assert store.recommendations.pool('home', 'all', 'wheelhouse')['eligible_count'] == 3
    assert [(row['key'], row['rank']) for row in store.recommendations.page('home', 'all', 'wheelhouse')] == [('one', 0), ('two', 2)]
    unknown = store.conn.execute('SELECT raw_key,rank,score,reason FROM unresolved_recommendation_candidates').fetchone()
    assert tuple(unknown) == ('removed-old-key', 1, .8, 'missing_recording')
    assert store.conn.execute("SELECT 1 FROM recordings WHERE identity_key='removed-old-key'").fetchone() is None
    pools.verify(store.conn, store.recommendations.pool('home', 'all', 'wheelhouse')['id'], prepared.slots[0].pool)
    with pytest.raises(sqlite3.IntegrityError), store.conn:
        store.conn.execute('UPDATE unresolved_recommendation_candidates SET rank=0')


def test_recommendation_generation_protects_group_history_without_a_permanent_parent_chain(recommendations):
    store = recommendations
    prepared = plan(store)
    publish(store, prepared)
    enable(store)
    from yt_playlist.core.group_reads import retention_closure
    assert 1 in retention_closure(store.conn)[0]
    drain(store)
    assert store.taste_groups.definition('first-leaf', generation='first') is not None
    publish(store, replace(plan(store, 'two'), group_generation_id=2))
    collect(store)
    drain(store)
    assert store.taste_groups.definition('first-leaf') is None


def test_prepared_context_can_retain_recommendations_without_group_roots(recommendations):
    store = recommendations
    publish(store, plan(store))
    generation = store.recommendations.head('home')['id']
    store.group_evidence.replace_prepared('home_prepared', {'context': {'recommendation_generations': [generation]}}, 10)
    identity = 'proposal:home_prepared:context'
    assert [row[0] for row in store.conn.execute('SELECT generation_id FROM recommendation_generation_refs '
        "WHERE kind='prepared' AND external_key=?", (identity,))] == [generation]
    assert not store.conn.execute('SELECT 1 FROM group_evidence_owners WHERE external_key=?', (identity,)).fetchone()


def test_output_files_survive_old_reader_and_backup_relocates_exact_owners(recommendations, tmp_path):
    store = recommendations
    first = publish(store, plan(store))
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        assert reader.recommendations.output('home', 'framing') == {'title': 'one'}
        publish(store, plan(store, 'two'))
        collect(store)
        assert outputs.collect_orphans(store.conn) == 0
        assert reader.recommendations.output('home', 'framing', generation=first.generation_id) == {'title': 'one'}
    assert outputs.collect_orphans(store.conn) == 1
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        manifest = outputs.export(reader.conn, tmp_path / 'recommendation-backup')
    before = [tuple(row) for row in store.conn.execute('SELECT * FROM prepared_outputs')]
    outputs.restore(store.conn, manifest)
    assert [tuple(row) for row in store.conn.execute('SELECT * FROM prepared_outputs')] == before
    assert store.recommendations.output('home', 'framing') == {'title': 'two'}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('candidate', [pools.Candidate(True), pools.Candidate(1, float('nan')),
    pools.Candidate(1, float('inf')), pools.Candidate(1, 1, True, 'genre'), pools.Candidate(1, 1, 51, 'genre')])
def test_invalid_candidate_rejected_before_any_database_write(candidate):
    with pytest.raises(ValueError):
        pools.prepare('pool', 'policy', None, 'a' * 64, 'radio', [candidate])
