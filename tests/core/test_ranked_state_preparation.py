"""Ranked preparation preserves whole candidate sets without serving cutover."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import struct

import pytest

from tests.comparison_support import legacy_projection
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import (
    guarded_numbered_store as guarded_installed,
    numbered_store,
    numbered_store as installed,
    preparation_ddl_connection,
    publication_consumer_reads,
)
from yt_playlist.core import fao, library_pool_state, model_projections, recommendation_outputs as files
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import ranked_state as preparation
from yt_playlist.core.store import Store
from yt_playlist.rec import comparison_storage
from yt_playlist.repos import recommendation_publication


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Ranked preparation exercises real SQL/files without HTTP startup."""


def finish(job, conn, size=128):
    for _ in range(300):
        progress = job.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Ranked preparation did not finish bounded fixture')


def seed(store, count=4, *, lane_counts=None, surfaces=None):
    seed_publication_subjects(store, [{'key': 'known|artist', 'video_id': 'known-video',
                                     'title': 'Known', 'artist': 'Artist', 'duration_s': 100}])
    store.rec_ledger.record_serving_policy('ranked-policy', {'algorithm': 'captured'}, 1)
    run_id = store.model_selection.start_selection(1, {'kind': 'product'})
    def candidates(amount):
        return [{'key': 'known|artist' if index == 0 else f'missing:{index}', 'score': 1 / (index + 1),
                 'pool_policy': 'ranked-policy', 'reason': '東京' if index == 0 else 'historical'}
                for index in range(amount)]

    items = candidates(count)
    keys = [item['key'] for item in items]
    radio = {'policy_version': 'ranked-policy', 'encoding_model': None,
        'modes': [{'mode_id': 12345, 'space': '', 'centroid': [1., -0., .25], 'label': 'Original mode'}],
        'bundles': {'12345': {'wheelhouse': items}, '_meta': {'year_cuts': [1990, 2010], 'full_pools': True},
            '_radio': {'generation': 'captured', 'space': '', 'members': {'12345': keys},
                       'scores': {key: .5 for key in keys}}, '_ppr': {'12345': list(reversed(keys))}}}
    values = {}
    for arm in ('incumbent', 'challenger'):
        for format in ('mixes', 'albums', 'radio'):
            value = radio if format == 'radio' else {'items': [], 'context': {}, 'caption': '東京'}
            surface = f'model_pair:{run_id}:{arm}:{format}'
            values[surface] = (json.dumps(legacy_projection(run_id, value)) if arm == 'challenger'
                               else json.dumps({'run_id': run_id, 'data': value}, ensure_ascii=False, indent=2))
    for lane in library_pool_state.LANES:
        lane_items = candidates(lane_counts[lane]) if lane_counts and lane in lane_counts else items
        values['home_pool:' + lane] = json.dumps({'parameters': {'lane': lane, 'epoch': 0,
            'include_genres': ['rock', 'other:Unregistered style'], 'avoid_genres': []}, 'tracks': lane_items}, indent=2)
    if surfaces is not None:
        values = {surface: raw for surface, raw in values.items()
                  if surface.split(':', 2)[-1] in surfaces}
    with store.conn:
        store.conn.execute("INSERT INTO genre_family(key,name) VALUES ('rock','Rock') ON CONFLICT DO NOTHING")
        store.conn.executemany('INSERT INTO rec_proposals VALUES (?,?,?)', [(surface, raw, None) for surface, raw in values.items()])
    return values, run_id


def rows(conn, surface, suffix, fields='*'):
    head = preparation.head(conn, surface)
    return list(map(tuple, conn.execute(f'SELECT {fields} FROM {preparation.table(surface,suffix)} '
        'WHERE surface=? AND preparation_revision=? ORDER BY 1,2', (surface, head['preparation_revision']))))


@pytest.mark.parametrize('installed', [79, 93, 94], indirect=True)
def test_all_six_projections_and_four_lanes_preserve_sources_and_unknown_clocks(installed, preparation_ddl_connection):
    store, _ = installed
    originals, run_id = seed(store)
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn, size=3)
    for surface, raw in originals.items():
        head = preparation.head(store.conn, surface)
        assert head['built_at'] is None
        assert preparation.read_original(store.conn, head) == raw
        assert store.rec_proposals.get_proposals(surface) == json.loads(raw)
        assert not store.conn.execute('SELECT 1 FROM recommendation_heads WHERE surface GLOB ?', (surface + '*',)).fetchone()
        assert all(row[2].startswith('prepared_') for row in store.conn.execute(
            f'PRAGMA foreign_key_list({preparation.table(surface,"generations")})'))
        if surface.endswith(':radio'):
            assert (head['run_id'], head['run_ref'], head['run_resolution']) == (run_id, run_id, None)
            mode = rows(store.conn, surface, 'modes', 'mode_ref,resolution_reason,space,centroid')[0]
            assert mode[:3] == (None, 'missing_mode', '')
            assert struct.unpack('<ddd', mode[3]) == (1., -0., .25)
            assert len(rows(store.conn, surface, 'candidates', 'pool_digest,rank')) == 16
        elif surface.startswith('home_pool:'):
            assert (head['lane'], head['epoch']) == (surface.split(':')[1], 0)
            assert rows(store.conn, surface, 'genres', 'position,raw_key,family_key,genre_key,resolution_reason') == [
                (0, 'rock', 'rock', None, None), (1, 'other:Unregistered style', None, None, 'unknown_genre')]
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    regular.migrate(preparation_ddl_connection)


def prepare_native_comparison(store):
    originals, _ = seed(store)
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn)
    return originals


@pytest.mark.parametrize('numbered_store', [79, 94], indirect=True)
def test_frozen_upgrade_matches_complete_ranked_slots_and_mode_bytes(numbered_store):
    store, _ = numbered_store
    conn = store.conn
    originals = prepare_native_comparison(store)
    expected = {}
    # Capture the actual prepared rows before conversion. One database and one
    # full registered upgrade retain every original byte/slot comparison.
    for surface, raw in originals.items():
        head = preparation.head(conn, surface)
        assert preparation.read_original(conn, head) == raw
        item = expected[surface] = {'head': dict(head)}
        if surface.endswith(':radio'):
            item['mode'] = rows(conn, surface, 'modes', 'mode_id,space,centroid')[0]
        if surface.endswith(':radio') or surface.startswith('home_pool:'):
            item['slots'] = rows(conn, surface, 'slots', 'slot_surface,mode_key,purpose,pool_digest')
            item['pools'] = {digest: list(map(tuple, conn.execute(
                f'SELECT rank,recording_id,raw_key,score,intensity_band,genre_key '
                f'FROM {preparation.table(surface,"candidates")} '
                'WHERE surface=? AND preparation_revision=? AND pool_digest=? ORDER BY rank',
                (surface, head['preparation_revision'], digest))))
                for digest, in conn.execute(f'SELECT content_digest FROM {preparation.table(surface,"pools")} '
                    'WHERE surface=? AND preparation_revision=?', (surface, head['preparation_revision']))}
    # Serving adapters belong to the predecessor layout; migrations use a bare
    # connection, just as the explicit upgrade command does.
    for name, in conn.execute("SELECT name FROM sqlite_temp_schema WHERE type='view'").fetchall():
        conn.execute('DROP VIEW temp."' + name.replace('"', '""') + '"')
    runner.migrate(conn, registry=registry.MIGRATIONS)
    for surface, raw in originals.items():
        assert preparation.read_original(conn, preparation.head(conn, surface)) == raw
        staged = expected[surface]['head']
        if surface.endswith(':radio'):
            native = conn.execute('SELECT * FROM model_pair_publications WHERE run_id=? AND arm=? AND format=?',
                (staged['run_id'], staged['arm'], staged['format'])).fetchone()
            assert native['built_at'] is None
            assert tuple(conn.execute('SELECT mode_id,space,centroid FROM model_pair_modes WHERE publication_id=?',
                (native['id'],)).fetchone()) == expected[surface]['mode']
            generation = native['generation_id']
        elif surface.startswith('home_pool:'):
            generation = conn.execute('SELECT generation_id FROM recommendation_heads WHERE surface=?', (surface,)).fetchone()[0]
        else:
            value = {'run_id': staged['run_id'], 'data': comparison_storage.decode(json.loads(raw))}
            assert model_projections.read(conn, surface) == value
            continue
        native_slots = list(map(tuple, conn.execute('SELECT s.surface,s.mode_key,s.purpose,p.content_digest '
            'FROM recommendation_surface_pools s JOIN recommendation_pools p ON p.id=s.pool_id '
            'WHERE s.generation_id=? ORDER BY 1,2', (generation,))))
        assert native_slots == expected[surface]['slots']
        for digest, candidates in expected[surface]['pools'].items():
            pool_id = conn.execute('SELECT id FROM recommendation_pools WHERE content_digest=?', (digest,)).fetchone()[0]
            native = list(map(tuple, conn.execute('SELECT c.rank,c.recording_id,NULL,c.score,b.intensity_band,b.genre_key '
                'FROM recommendation_candidates c LEFT JOIN recommendation_radio_bands b ON b.pool_id=c.pool_id AND b.recording_id=c.recording_id '
                'WHERE c.pool_id=? UNION ALL SELECT rank,NULL,raw_key,score,intensity_band,genre_key '
                'FROM unresolved_recommendation_candidates WHERE pool_id=? ORDER BY rank', (pool_id, pool_id))))
            assert native == candidates
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_large_ranked_set_resumes_from_files_and_keeps_last_complete_heads(installed, monkeypatch):
    store, _ = installed
    originals, run_id = seed(store, count=515)
    job = preparation.JOBS['pairs']
    job.install(store.conn)
    calls, real_prepare = [], model_projections.prepare
    def counted(*args, **kwargs):
        assert not store.conn.in_transaction
        calls.append(args[1])
        return real_prepare(*args, **kwargs)
    monkeypatch.setattr(model_projections, 'prepare', counted)
    finish(job, store.conn)
    assert len(calls) == 6
    surface = f'model_pair:{run_id}:challenger:radio'
    previous = preparation.head(store.conn, surface)
    assert len(rows(store.conn, surface, 'candidates', 'pool_digest,rank')) == 2060
    with store.conn:
        store.conn.execute('UPDATE rec_proposals SET built_at=22 WHERE surface=?', (surface,))
    before_calls = len(calls)
    for _ in range(40):
        progress = job.step(store.conn, batch_size=128)
        if progress.status == 'prepared':
            break
        assert preparation.head(store.conn, surface)['preparation_revision'] == previous['preparation_revision']
    assert progress.status == 'prepared' and len(calls) == before_calls + 1
    assert preparation.head(store.conn, surface)['built_at'] == 22
    assert preparation.read_original(store.conn, previous) == originals[surface]
    assert store.conn.execute(f'SELECT count(*) FROM {preparation.table(surface,"candidates")} WHERE surface=?', (surface,)).fetchone()[0] == 4120


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_continuing_inputs_reject_stale_plan_and_reconcile_resolved_identities(installed, monkeypatch):
    store, _ = installed
    _, run_id = seed(store)
    job = preparation.JOBS['pairs']
    job.install(store.conn)
    publish, changed = files.publish, False
    def during_files(root, encoded):
        nonlocal changed
        assert not store.conn.in_transaction
        if not changed:
            changed = True
            store.tracks.upsert_track('arrived', 'Arrived', 'Artist', None, 100)
        return publish(root, encoded)
    monkeypatch.setattr(files, 'publish', during_files)
    assert job.step(store.conn).processed == 0
    assert not store.conn.execute('SELECT 1 FROM prepared_model_pairs_heads').fetchone()
    finish(job, store.conn)
    surface = f'model_pair:{run_id}:incumbent:radio'
    old = preparation.head(store.conn, surface)
    with store.conn:
        store.conn.execute("INSERT INTO recordings(identity_key) VALUES ('missing:1')")
        store.conn.execute('DELETE FROM rec_model_selection_runs WHERE id=?', (run_id,))
    finish(job, store.conn)
    current = preparation.head(store.conn, surface)
    assert (current['run_ref'], current['run_resolution']) == (None, 'missing_run')
    assert store.conn.execute('SELECT count(*) FROM prepared_model_pairs_candidates WHERE surface=? '
        'AND preparation_revision=? AND raw_key=?', (surface, current['preparation_revision'], 'missing:1')).fetchone()[0] == 0
    assert preparation.read_original(store.conn, old) == preparation.read_original(store.conn, current)
    with store.conn:
        store.conn.execute('DELETE FROM rec_proposals WHERE surface=?', (surface,))
    finish(job, store.conn)
    assert preparation.head(store.conn, surface) is None
    assert preparation.read_original(store.conn, old)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_interrupt_failure_retry_and_independent_regular_ddl(installed, preparation_ddl_connection, monkeypatch):
    store, _ = installed
    seed(store)
    pairs, clicked = preparation.JOBS.values()
    for job in (pairs, clicked):
        job.install(store.conn)
    checkpoint = pairs.step(store.conn, batch_size=1)
    tables = [name for name, sql in pairs.definition.destinations if sql.startswith('CREATE TABLE')]
    retained = {name: list(map(tuple, store.conn.execute('SELECT * FROM ' + name))) for name in tables}
    def interrupt(conn, row, revision, selected):
        preparation.copy_batch(conn, row, revision, selected)
        raise KeyboardInterrupt('controlled ranked interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(pairs, copy_batch=interrupt).step(store.conn, batch_size=128)
    assert pairs.progress(store.conn) == checkpoint
    assert {name: list(map(tuple, store.conn.execute('SELECT * FROM ' + name))) for name in tables} == retained
    assert not store.conn.in_transaction
    def failure(*args):
        assert not store.conn.in_transaction
        raise OSError('controlled ranked disk failure')
    publish = files.publish
    monkeypatch.setattr(files, 'publish', failure)
    with pytest.raises(OSError, match='disk failure'):
        clicked.step(store.conn)
    failed = clicked.progress(store.conn)
    monkeypatch.setattr(files, 'publish', publish)
    finish(pairs, store.conn)
    def install(conn, context):
        conn.execute('CREATE TABLE independent_ranked_ddl(value TEXT)')
    def validate(conn):
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='independent_ranked_ddl'").fetchone()
    monkeypatch.setattr(regular, 'CHANGES', (*regular.CHANGES, regular.Change(len(regular.CHANGES) + 1,
        'independent_ranked_ddl', install, validate)))
    regular.migrate(preparation_ddl_connection)
    assert clicked.progress(store.conn) == failed
    assert store.conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == len(regular.CHANGES)
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            clicked.step(reopened.conn)
        clicked.step(reopened.conn, retry=True)
        finish(clicked, reopened.conn)
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [85], indirect=True)
def test_optional_redirect_capture_is_added_after_genuine_upgrade_and_withdraws_old_proof(installed, tmp_path):
    store, _ = installed
    seed(store)
    with store.conn:
        store.conn.executemany("INSERT INTO genre_catalog(key,name,source) VALUES (?,?,'custom')", [('old-style', 'Old Style'), ('new-style', 'New Style')])
        raw = json.loads(store.conn.execute("SELECT payload FROM rec_proposals WHERE surface='home_pool:explore'").fetchone()[0])
        raw['parameters']['include_genres'] = ['old-style']
        store.conn.execute("UPDATE rec_proposals SET payload=? WHERE surface='home_pool:explore'", (json.dumps(raw),))
    job = preparation.JOBS['clicked']
    job.install(store.conn)
    finish(job, store.conn)
    upgraded = Store(tmp_path / 'optional-redirect-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:86])
        before = job.progress(upgraded.conn)
        with pytest.raises(MigrationError, match='Incompatible preparation object'):
            job.step(upgraded.conn)
        assert job.progress(upgraded.conn) == before
        current = job.install(upgraded.conn)
        assert current.prepared_revision is None and current.input_revision == before.input_revision + 1
        finish(job, upgraded.conn)
        with upgraded.conn:
            upgraded.conn.execute("INSERT INTO genre_redirects VALUES ('old-style','new-style')")
        assert job.progress(upgraded.conn).prepared_revision is None
        finish(job, upgraded.conn)
        assert rows(upgraded.conn, 'home_pool:explore', 'genres', 'raw_key,genre_key,resolution_reason') == [('old-style', 'new-style', None)]
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        upgraded.conn.close()


def test_observationless_capture_cannot_be_admitted_as_native_generation():
    plan = recommendation_publication.Plan('captured', 'policy', None, None, None,
        outputs=(recommendation_publication.Output('home', 'empty', files.encode('home', 'empty', {})),))
    assert recommendation_publication.validate(plan, allow_unobserved=True)
    with pytest.raises(ValueError, match='Invalid recommendation publication plan'):
        recommendation_publication.validate(plan)


@pytest.mark.parametrize('guarded_installed', [79], indirect=True)
def test_roots_preserve_real_generations_versions_and_releases_in_bounded_batches(guarded_installed):
    from tests.group_support import publish as publish_groups, recordings
    from tests.repos.test_typed_recommendation_publication import plan, publish
    from yt_playlist.core import group_evidence, recommendation_custody
    store, _ = guarded_installed
    _, run_id = seed(store)
    tracks = recordings([('House', 12, .3)])
    with store.conn:
        store.conn.executemany('INSERT INTO recordings(identity_key) VALUES (?)', [(track.key,) for track in tracks])
    groups = publish_groups(store, tracks)
    generation = publish(store, plan(store)).generation_id
    context = {'group_generation': groups['generation'], 'musical_groups': [
        {'version': groups['groups'][0]['version'], 'group_id': groups['groups'][0]['group_id']}]}
    key = f'proposal:model_pair:{run_id}:incumbent:radio:context'
    with store.conn.atomic():
        group_evidence.retain(store.conn, 'comparison', key, context, 1000)
        recommendation_custody.retain(store.conn, 'comparison', key, generation)
    originals = {owner: [dict(row) for row in store.conn.execute('SELECT * FROM ' + owner +
        " WHERE kind='comparison' AND external_key GLOB 'proposal:model_pair:*'")] for owner in preparation.home_preparation.ROOT_KEYS}
    pairs, roots = preparation.JOBS['pairs'], preparation.ROOT_JOB
    pairs.install(store.conn)
    roots.install(store.conn)
    with pytest.raises(MigrationError, match='reconciled output files'):
        roots.step(store.conn)
    finish(pairs, store.conn)
    roots.step(store.conn, retry=True, batch_size=1)
    finish(roots, store.conn, size=1)
    for owner, expected in originals.items():
        archived = [json.loads(row[0]) for row in store.conn.execute(
            f'SELECT source_snapshot FROM {preparation.ORIGINALS} WHERE owner=?', (owner,))]
        assert sorted(archived, key=str) == sorted(expected, key=str)
    assert originals['group_evidence_generations'] and originals['group_evidence_versions'] and originals['recommendation_generation_refs']
    with store.conn.atomic():
        group_evidence.release(store.conn, 'comparison', key)
        recommendation_custody.release(store.conn, 'comparison', key)
        group_evidence.retain_unknown_context(store.conn, 'comparison', key, 'original:unavailable', 'missing_context', 2000)
    assert roots.progress(store.conn).prepared_revision is None
    finish(roots, store.conn)
    assert store.conn.execute(f'SELECT source_context FROM {preparation.ROOTS} WHERE owner=?',
        ('unresolved_group_contexts',)).fetchone()[0] == 'original:unavailable'
    assert not store.conn.execute(f'SELECT 1 FROM {preparation.ROOTS} WHERE owner=?', ('recommendation_generation_refs',)).fetchone()
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('kind', ['address', 'candidate', 'codec', 'policy', 'size'])
def test_invalid_source_fails_closed_retains_authoritative_bytes_and_does_not_block_regular_ddl(installed, preparation_ddl_connection, monkeypatch, kind):
    store, _ = installed
    originals, run_id = seed(store)
    surface = f'model_pair:{run_id}:incumbent:radio'
    value = json.loads(originals[surface])
    if kind == 'address':
        surface = 'model_pair:01:incumbent:radio'
    elif kind == 'candidate':
        items = value['data']['bundles']['12345']['wheelhouse']
        items.append(items[0])
    elif kind == 'codec':
        value = {'run_id': run_id, 'codec': 'unknown', 'expanded_bytes': 1, 'data': ''}
    elif kind == 'policy':
        value['data']['policy_version'] = 'missing-policy'
    else:
        monkeypatch.setattr(preparation, 'MAX_SOURCE_BYTES', 1)
    raw = json.dumps(value)
    with store.conn:
        store.conn.execute('DELETE FROM rec_proposals')
        store.conn.execute('INSERT INTO rec_proposals VALUES (?,?,NULL)', (surface, raw))
    job = preparation.JOBS['pairs']
    job.install(store.conn)
    with pytest.raises((ValueError, MigrationError)):
        job.step(store.conn)
    assert job.progress(store.conn).status == 'failed'
    assert not store.conn.execute('SELECT 1 FROM prepared_model_pairs_heads').fetchone()
    assert store.conn.execute('SELECT payload FROM rec_proposals WHERE surface=?', (surface,)).fetchone()[0] == raw
    regular.migrate(preparation_ddl_connection)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_empty_clicked_pool_keeps_parameters_without_inventing_policy_or_generation(installed):
    store, _ = installed
    value = {'parameters': {'lane': 'comfort', 'epoch': 0, 'include_genres': [], 'avoid_genres': ['unknown']}, 'tracks': []}
    store.rec_proposals.put_proposals('home_pool:comfort', value, None)
    job = preparation.JOBS['clicked']
    job.install(store.conn)
    finish(job, store.conn)
    head = preparation.head(store.conn, 'home_pool:comfort')
    assert head['ranked_digest'] is None and head['ranked_policy'] is None and head['built_at'] is None
    assert (head['lane'], head['epoch']) == ('comfort', 0)
    assert rows(store.conn, 'home_pool:comfort', 'genres', 'raw_key,resolution_reason') == [('unknown', 'unknown_genre')]
    assert json.loads(preparation.read_original(store.conn, head)) == value


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_missing_durable_plan_requires_explicit_retry_and_rebuilds_exact_checkpoint(installed, monkeypatch):
    store, _ = installed
    seed(store)
    job = preparation.JOBS['pairs']
    job.install(store.conn)
    checkpoint = job.step(store.conn, batch_size=1)
    generation = store.conn.execute('SELECT * FROM prepared_model_pairs_generations').fetchone()
    filename = Path(generation['plan_filename'])
    original = fao.read(filename).bytes
    filename.unlink()
    with pytest.raises(FileNotFoundError):
        job.step(store.conn, batch_size=1)
    assert job.progress(store.conn).status == 'failed'
    assert job.progress(store.conn).processed == checkpoint.processed
    read = files.read
    def outside_sql(*args, **kwargs):
        assert not store.conn.in_transaction
        return read(*args, **kwargs)
    monkeypatch.setattr(files, 'read', outside_sql)
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            job.step(reopened.conn)
        job.step(reopened.conn, batch_size=1, retry=True)
        finish(job, reopened.conn)
        assert fao.read(filename).bytes == original
        assert preparation.read_original(reopened.conn, generation)
    finally:
        reopened.conn.close()


def test_durable_plan_pages_its_index_and_reads_only_the_selected_member_files(tmp_path, monkeypatch):
    store = Store(tmp_path / 'paged-plan.db')
    surface = 'model_pair:1:incumbent:mixes'
    try:
        count = 128 * 129
        values = [('context', ('context', str(position))) for position in range(count)]
        plan = preparation.durable_plan(store.conn, surface, {}, values)
        manifest = files.read(*plan.manifest, surface=surface, chunk_key='plan')
        assert len(manifest['indexes']) == 2
        loaded = preparation.Units(surface, manifest['indexes'], manifest['fields']['unit_count'])
        read, calls = files.read, []
        def selected(*args, **kwargs):
            assert not store.conn.in_transaction
            calls.append(kwargs['chunk_key'])
            return read(*args, **kwargs)
        monkeypatch.setattr(files, 'read', selected)
        for position in (0, 1, 128, 128 * 128 - 1, 128 * 128, count - 1):
            assert loaded[position] == ['context', ['context', str(position)]]
        assert calls.count('index') == 2 and calls.count('units') == 4
    finally:
        store.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_resumed_batch_reads_neither_full_source_nor_whole_plan(installed):
    store, _ = installed
    seed(store)
    job = preparation.JOBS['pairs']
    job.install(store.conn)
    checkpoint = job.step(store.conn, batch_size=1)
    denied = []
    def no_source_payload(operation, owner, column, database, trigger):
        if operation == sqlite3.SQLITE_READ and owner == 'rec_proposals' and column == 'payload':
            denied.append(column)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    store.conn.set_authorizer(no_source_payload)
    try:
        result = job.step(store.conn, batch_size=1)
        assert result.processed == checkpoint.processed + 1
        assert not denied
    finally:
        store.conn.set_authorizer(None)
