"""Bounded controlled updates on one isolated, already-current populated library.

This is publication/storage acceptance, not a model fit or process-cold benchmark.
The group continuation runs ordinary corpus grouping after one controlled accepted
feature change. The module fixture makes one isolated copy for both checks.
"""
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import resource
import sqlite3
import sys
import time
import traceback

import pytest

from tests.schema import populated_scope_restart
from yt_playlist.core import (
    group_documents, group_storage, prepared_inputs,
    recommendation_outputs, recommendation_pools,
)
from yt_playlist.core.activity import WorkBudgetExceeded, work_budget
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.migrations import current
from yt_playlist.core.store import Store
from yt_playlist.rec import group_preparation, group_roots, group_similarity, group_versions, mode_surfaces
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.repos import compact_group_publication, group_publication
from yt_playlist.repos import recommendation_publication as publication
from yt_playlist.repos.rec_proposals import RecProposalsRepo
from yt_playlist.repos.recommendation_publications import RecommendationPublicationsRepo
from yt_playlist.util import genre_catalog, genre_map


ROOT = Path(__file__).resolve().parents[2]


def _sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _sources():
    paths = (
        'core/store.py', 'core/db_reads.py', 'core/db_rebuild/sqlite_checks.py',
        'core/recommendation_outputs.py', 'core/recommendation_pools.py',
        'core/migrations/current.py', 'core/migrations/registry.py',
        'core/migrations/runner.py', 'core/migrations/schema.sql',
        'core/migrations/v073_recommendation_publications.py',
        'repos/recommendation_publication.py', 'repos/recommendation_publications.py',
        'repos/base.py', 'repos/rec_proposals.py', 'rec/stored_home.py',
        'rec/stored_radio.py', 'rec/mode_surfaces.py', 'rec/radio_candidates.py',
        'core/activity.py', 'core/db_snapshot.py', 'core/db_revisions.py', 'core/read_memo.py',
        'core/group_content.py', 'core/group_documents.py', 'core/group_storage.py',
        'core/group_evidence.py', 'core/prepared_inputs.py', 'core/policy_documents.py',
        'repos/prepared_content.py', 'repos/taste_groups.py', 'repos/compact_taste_groups.py',
        'repos/group_queries.py', 'repos/group_publication.py', 'repos/group_retention.py',
        'repos/compact_group_publication.py', 'rec/group_preparation.py',
        'rec/group_discovery.py', 'rec/group_similarity.py', 'rec/group_versions.py',
        'rec/group_names.py', 'rec/musical_profiles.py', 'rec/group_roots.py',
        'rec/recommendation_roots.py',
        'util/genre_catalog.py', 'util/genre_map.py',
        'data/musicbrainz_genres.json', 'data/musicbrainz_genre_parents.json', 'data/genre_families.json',
        'core/album_memberships.py', 'core/playlist_scope_state.py', 'core/playlist_scopes.py',
        'repos/playlists.py', 'repos/overlaps.py', 'library/playlist_metadata.py',
    )
    result = {name: _sha(ROOT / 'src/yt_playlist' / name) for name in paths}
    result.update({str(path.relative_to(ROOT)): _sha(path) for path in current.DEFINITIONS})
    result[str(Path(__file__).relative_to(ROOT))] = _sha(__file__)
    fixture_helper = ROOT / 'tests/file_custody_support.py'
    result[str(fixture_helper.relative_to(ROOT))] = _sha(fixture_helper)
    restart_helper = Path(populated_scope_restart.__file__).resolve()
    result[str(restart_helper.relative_to(ROOT))] = _sha(restart_helper)
    return result


@pytest.fixture(scope='module')
def isolated_current_library(request, tmp_path_factory):
    """Read the immutable retained donor; relocate all owned files on one copy."""
    option = request.config.getoption('--schema-upgraded-db')
    if not option:
        pytest.skip('Supply --schema-upgraded-db with the completed #491 populated continuation')
    donor = Path(option).resolve(strict=True)
    assert not Path(str(donor) + '-wal').exists(), 'Use an offline checkpointed donor'
    digest = _sha(donor)
    output = tmp_path_factory.mktemp('populated-recommendation-continuations')
    db_path = output / 'populated.db'
    source = sqlite3.connect(donor.as_uri() + '?mode=ro&immutable=1', uri=True)
    source.row_factory = sqlite3.Row
    target = sqlite3.connect(db_path)
    target.row_factory = sqlite3.Row
    report = {'donor': str(donor), 'donor_sha256': digest, 'database': str(db_path),
              'python': sys.version, 'sqlite': sqlite3.sqlite_version, 'status': 'copying',
              'sources_before': _sources()}
    try:
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        report['recordings'] = source.execute('SELECT count(*) FROM recordings').fetchone()[0]
        assert report['recordings'] >= 10000, 'This check requires the retained actual populated library'
        report['donor_schema'] = source.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
        assert report['donor_schema'] == current.VERSION, (
            'Use an already-current populated output; this continuation must not repeat a schema upgrade')
        assert source.execute("SELECT 1 FROM sqlite_schema WHERE name='entry_artists'").fetchone(), (
            'Use the completed normal-startup v79 output, not another migration predecessor')
        started = time.monotonic()
        source.backup(target)
        from tests import file_custody_support as custody
        manifests = custody.export_files(source, db_path)
        # Commit every relocated file owner before closing this connection or
        # allowing either continuation to open the disposable database.
        with target:
            custody.restore_files(target, manifests)
        report.update(copy_and_relocation_seconds=time.monotonic() - started,
                      relocated_domains=sorted(manifests), status='ready')
    finally:
        target.close()
        source.close()
        report['donor_unchanged_after_copy'] = _sha(donor) == digest
        report['sources_unchanged_after_copy'] = _sources() == report['sources_before']
        if not report['donor_unchanged_after_copy']:
            report['status'] = 'failed_donor_guard'
        elif not report['sources_unchanged_after_copy']:
            report['status'] = 'failed_source_guard'
        (output / 'library-custody.json').write_text(json.dumps(report, indent=2) + '\n')
        assert report['donor_unchanged_after_copy'], 'The retained donor changed during copying'
        assert report['sources_unchanged_after_copy'], 'Reviewed sources changed during copying'
    try:
        yield db_path, output
    finally:
        report['donor_unchanged'] = _sha(donor) == digest
        report['sources_unchanged'] = _sources() == report['sources_before']
        report['status'] = ('failed_donor_guard' if not report['donor_unchanged'] else
                            'failed_source_guard' if not report['sources_unchanged'] else 'finished')
        (output / 'library-custody.json').write_text(json.dumps(report, indent=2) + '\n')
        assert report['donor_unchanged'], 'The retained donor changed'
        assert report['sources_unchanged'], 'Reviewed sources changed during acceptance'


def _heads(conn):
    return dict(conn.execute('SELECT surface,generation_id FROM recommendation_heads ORDER BY surface'))


def _slots(conn):
    return {(r[0], r[1], r[2]): r[3] for r in conn.execute(
        'SELECT m.surface,m.mode_key,m.purpose,m.pool_id FROM recommendation_heads h '
        'JOIN recommendation_surface_pools m ON m.generation_id=h.generation_id AND m.surface=h.surface '
        'ORDER BY m.surface,m.mode_key,m.purpose')}


def _rows(conn, identity):
    return conn.execute(
        "SELECT 'recording',c.recording_id,c.rank,c.score,b.intensity_band,b.genre_key "
        'FROM recommendation_candidates c LEFT JOIN recommendation_radio_bands b '
        'ON b.pool_id=c.pool_id AND b.recording_id=c.recording_id WHERE c.pool_id=? '
        "UNION ALL SELECT 'unresolved',raw_key,rank,score,intensity_band,genre_key "
        'FROM unresolved_recommendation_candidates WHERE pool_id=? ORDER BY 3', (identity, identity))


def _pool_fingerprint(conn, identity):
    digest = hashlib.sha256()
    header = conn.execute('SELECT * FROM recommendation_pools WHERE id=?', (identity,)).fetchone()
    digest.update(canonical(tuple(header)))
    count = 0
    for row in _rows(conn, identity):
        digest.update(canonical(tuple(row)))
        count += 1
    return {'rows': count, 'sha256': digest.hexdigest()}


def _prepared_pool(conn, identity):
    header = dict(conn.execute('SELECT * FROM recommendation_pools WHERE id=?', (identity,)).fetchone())
    values = []
    for kind, owner, rank, score, band, genre in _rows(conn, identity):
        assert rank == len(values), 'The existing actual pool must have complete ordered ranks'
        cls = recommendation_pools.Candidate if kind == 'recording' else recommendation_pools.UnresolvedCandidate
        values.append(cls(owner, score, band, genre))
    prepared = recommendation_pools.prepare(header['pool_key'], header['policy_version'],
        header['group_version_id'], header['input_fingerprint'], header['purpose'], values)
    assert prepared.content_digest == header['content_digest']
    return prepared


def _allocation(conn):
    tables = ('recommendation_generations', 'recommendation_pools', 'recommendation_candidates',
              'unresolved_recommendation_candidates', 'recommendation_radio_bands',
              'recommendation_surface_pools', 'prepared_outputs')
    names = [row[0] for row in conn.execute(
        'SELECT name FROM sqlite_schema WHERE (type=\'table\' OR type=\'index\') AND tbl_name IN ('
        + ','.join('?' for _ in tables) + ')', tables)]
    return {
        'rows': {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in tables},
        'allocated_bytes': sum(row[0] for row in conn.execute(
            'SELECT pgsize FROM dbstat WHERE name IN (' + ','.join('?' for _ in names) + ')', names)),
        'database_file_bytes': Path(conn.execute('PRAGMA database_list').fetchone()[2]).stat().st_size,
        'page_count': conn.execute('PRAGMA page_count').fetchone()[0],
        'freelist_pages': conn.execute('PRAGMA freelist_count').fetchone()[0],
        'journal_mode': conn.execute('PRAGMA journal_mode').fetchone()[0],
    }


def _file_inventory(root):
    return {str(path.relative_to(root)): (path.stat().st_size, _sha(path))
            for path in root.glob('*/*.json')}


def test_one_actual_fresh_pool_update_preserves_other_pools_and_measures_serving(
        isolated_current_library, monkeypatch):
    db_path, output = isolated_current_library
    sources = _sources()
    assert sources == json.loads((output / 'library-custody.json').read_text())['sources_before']
    report = {'status': 'incomplete', 'controlled_score_change_on_actual_data': True,
              'python': sys.version, 'sqlite': sqlite3.sqlite_version, 'sources_before': sources,
              'limitations': ['Fresh publication only; no model refit or whole-bundle recomputation',
                              'Warm filesystem/process; measurements include observer overhead',
                              'SQL row/object and file growth are not physical device-write bytes']}
    store = Store(db_path)
    try:
        assert Path(sys.modules[Store.__module__].__file__).resolve() == ROOT / 'src/yt_playlist/core/store.py'
        # Reuse the completed #491 continuation and this one existing copy. The
        # snapshot precedes ordinary startup and preserves exact SQLite types.
        expected_scope = populated_scope_restart.snapshot(store.conn)
        started = time.monotonic()
        store.init_schema()
        report.update(startup_seconds=time.monotonic() - started, runtime_schema=current.VERSION)
        report['scope_initial_startup'] = populated_scope_restart.verify(store, expected_scope)
        conn = store.conn
        before_heads, before_slots = _heads(conn), _slots(conn)
        assert {'home', 'radio', 'fresh'} <= before_heads.keys(), 'Actual prepared surfaces are required'
        before_facts = {identity: _pool_fingerprint(conn, identity) for identity in set(before_slots.values())}
        report.update(actual_slots=len(before_slots), actual_distinct_pools=len(before_facts),
                      actual_distinct_rank_rows=sum(row['rows'] for row in before_facts.values()))
        selected_slot = ('fresh', 'all', 'fresh')
        assert selected_slot in before_slots, 'The actual Fresh pool must exist; do not fabricate a slot'
        old_id = before_slots[selected_slot]
        initial = store.recommendations.candidate_page(*selected_slot, limit=128)
        assert initial['tracks'], 'The existing actual Fresh page has no playable output'
        changed_recording = initial['tracks'][0]['recording_id']

        started = time.monotonic()
        surface_pools = {identity: _prepared_pool(conn, identity) for slot, identity in before_slots.items()
                         if slot[0] == 'fresh'}
        original_pool = surface_pools[old_id]
        index = next(i for i, candidate in enumerate(original_pool.candidates)
                     if isinstance(candidate, recommendation_pools.Candidate)
                     and candidate.recording_id == changed_recording)
        original_score = original_pool.candidates[index].score
        changed_score = .125 if original_score != .125 else .25
        values = list(original_pool.candidates)
        values[index] = replace(values[index], score=changed_score)
        changed_pool = recommendation_pools.prepare(original_pool.pool_key, original_pool.policy_version,
            original_pool.group_version_id, hashlib.sha256(canonical(
                ['review486-score-update', original_pool.input_fingerprint, changed_recording, changed_score])).hexdigest(),
            original_pool.purpose, values)
        slots = tuple(publication.Slot(*slot, changed_pool if slot == selected_slot else surface_pools[identity])
                      for slot, identity in before_slots.items() if slot[0] == 'fresh')
        chunks = []
        for row in conn.execute('SELECT * FROM prepared_outputs WHERE generation_id=? AND surface=? ORDER BY id',
                                (before_heads['fresh'], 'fresh')):
            encoded = recommendation_outputs._read(row['filename'], row['content_digest'], row['byte_size'],
                                                    surface='fresh', chunk_key=row['chunk_key'])
            key = row['chunk_key']
            digest = surface_pools[row['pool_id']].content_digest if row['pool_id'] is not None else None
            if row['pool_id'] == old_id:
                if any(slot != selected_slot and identity == old_id for slot, identity in before_slots.items()
                       if slot[0] == 'fresh'):
                    chunks.append(publication.Output('fresh', key, encoded, row['expires_at'], digest,
                                                     row['first_rank'], row['last_rank']))
                key = canonical(['pool-display', changed_pool.content_digest, row['first_rank']]).decode()
                encoded = recommendation_outputs.encode('fresh', key, json.loads(encoded)['output'])
                digest = changed_pool.content_digest
            chunks.append(publication.Output('fresh', key, encoded, row['expires_at'], digest,
                                             row['first_rank'], row['last_rank']))
        head = store.recommendations.head('fresh')
        plan = publication.Plan(f'review486-fresh:{time.time_ns()}', head['policy_version'],
            head['group_generation_id'], head['input_revision'], time.time(), slots, tuple(chunks))
        report.update(plan_preparation_seconds=time.monotonic() - started,
                      changed_slot=list(selected_slot), old_pool_id=old_id,
                      changed_recording=changed_recording, original_score=original_score,
                      changed_score=changed_score, changed_pool_rows=len(changed_pool.candidates),
                      publication_slots=len(slots), publication_chunks=len(chunks))

        files_root = recommendation_outputs.directory(conn)
        before_files, before_sql = _file_inventory(files_root), _allocation(conn)
        attempts = Counter()
        publish_file = recommendation_outputs.publish
        def observed_publish(root, encoded):
            attempts['calls'] += 1
            attempts['encoded_bytes'] += len(encoded)
            return publish_file(root, encoded)
        monkeypatch.setattr(recommendation_outputs, 'publish', observed_publish)
        dml = Counter()
        def authorize(action, table, _column, _database, _origin):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                dml[(action, table)] += 1
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        changes = conn.total_changes
        blocks = resource.getrusage(resource.RUSAGE_SELF).ru_oublock
        started = time.monotonic()
        try:
            build = store.recommendations.claim(plan)
            store.recommendations.stage(plan, build)
            assert _heads(conn) == before_heads, 'Hidden stage changed a serving head'
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                store.recommendations.publish(build, time.time())
        finally:
            conn.set_authorizer(None)
            report.update(publication_seconds=time.monotonic() - started,
                          sql_total_changes=conn.total_changes - changes,
                          process_output_blocks=resource.getrusage(resource.RUSAGE_SELF).ru_oublock - blocks,
                          prepared_dml_authorizations={f'{action}:{table}': count for (action, table), count in dml.items()},
                          output_publication_attempts=dict(attempts))
        after_files, after_sql = _file_inventory(files_root), _allocation(conn)
        assert all(after_files.get(path) == value for path, value in before_files.items())
        new_files = {path: value for path, value in after_files.items() if path not in before_files}
        report.update(sql_before=before_sql, sql_after=after_sql,
                      sql_row_deltas={table: after_sql['rows'][table] - count for table, count in before_sql['rows'].items()},
                      new_output_files=len(new_files), new_output_bytes=sum(value[0] for value in new_files.values()))
        after_heads, after_slots = _heads(conn), _slots(conn)
        assert after_heads == {**before_heads, 'fresh': build.generation_id}
        new_id = after_slots[selected_slot]
        assert new_id != old_id
        assert after_slots == {**before_slots, selected_slot: new_id}
        for identity, expected in before_facts.items():
            assert _pool_fingerprint(conn, identity) == expected, f'Original pool {identity} changed'
        expected_rows = len(changed_pool.candidates)
        assert report['sql_row_deltas']['recommendation_pools'] == 1
        assert (report['sql_row_deltas']['recommendation_candidates']
                + report['sql_row_deltas']['unresolved_recommendation_candidates']) == expected_rows
        report.update(new_pool_id=new_id, unrelated_heads_slots_and_original_full_content_preserved=True,
                      candidate_row_write_amplification=expected_rows,
                      held_original_pool_fingerprint=before_facts[old_id])

        store.conn.close()
        store = Store(db_path)
        started = time.monotonic()
        store.init_schema()
        report['reopen_seconds'] = time.monotonic() - started
        report['scope_reopen'] = populated_scope_restart.verify(store, expected_scope)
        assert _heads(store.conn) == after_heads
        assert _slots(store.conn) == after_slots

        # Install serving observers only after all preparation/publication work.
        # Count every real decode, including validation work.
        decoded, read_bytes, pages = Counter(), Counter(), Counter()
        report.update(serving_decode=decoded, serving_file_reads=read_bytes, serving_candidate_pages=pages)
        phase = ['fresh']
        decode, read = recommendation_outputs._decode, recommendation_outputs._read
        def observed_decode(encoded, *args, **kwargs):
            decoded[phase[0] + '_calls'] += 1
            decoded[phase[0] + '_bytes'] += len(encoded)
            return decode(encoded, *args, **kwargs)
        def observed_read(*args, **kwargs):
            encoded = read(*args, **kwargs)
            read_bytes[phase[0] + '_calls'] += 1
            read_bytes[phase[0] + '_bytes'] += len(encoded)
            return encoded
        original_page = RecommendationPublicationsRepo.candidate_page
        def observed_page(repo, *args, **kwargs):
            value = original_page(repo, *args, **kwargs)
            pages[phase[0] + '_calls'] += 1
            pages[phase[0] + '_hydrated'] += len(value['tracks'])
            pages[phase[0] + '_max_limit'] = max(pages[phase[0] + '_max_limit'], kwargs.get('limit', 128))
            assert len(value['tracks']) <= kwargs.get('limit', 128) <= 128
            return value
        old_proposal = RecProposalsRepo._proposal_json
        def no_bundle(repo, surface):
            assert surface not in ('mode_bundles', 'fresh_pool'), 'Serving decoded a retired full bundle'
            return old_proposal(repo, surface)
        monkeypatch.setattr(recommendation_outputs, '_decode', observed_decode)
        monkeypatch.setattr(recommendation_outputs, '_read', observed_read)
        monkeypatch.setattr(RecommendationPublicationsRepo, 'candidate_page', observed_page)
        monkeypatch.setattr(RecProposalsRepo, '_proposal_json', no_bundle)
        started = time.monotonic()
        fresh = store.recommendations.candidate_page(*selected_slot, limit=128)
        report['fresh_useful_output_seconds'] = time.monotonic() - started
        assert fresh['tracks'] and fresh['tracks'][0]['recording_id'] == changed_recording
        assert fresh['tracks'][0]['score'] == changed_score
        assert fresh['tracks'][0]['serving_context']['recommendation_pool_id'] == new_id
        now = time.time()
        phase[0] = 'home'
        started = time.monotonic()
        cards = mode_surfaces.assemble_cards(store, now, 0)
        report.update(home_useful_output_seconds=time.monotonic() - started,
                      home_cards=[{'lane': card['lane'], 'tracks': len(card['tracks'])} for card in cards])
        assert len(cards) == 4 and all(len(card['tracks']) == 12 for card in cards)
        phase[0] = 'radio'
        started = time.monotonic()
        radio = RadioCandidates(store, now, comparison=None)
        try:
            radio.rng.seed(0)
            seeds = radio.seed_offer(12)
        finally:
            radio.close()
        report.update(radio_useful_output_seconds=time.monotonic() - started,
                      radio_seed_keys=[track['key'] for track in seeds])
        assert len(seeds) == 12
        report.update(serving_decode=dict(decoded), serving_file_reads=dict(read_bytes),
                      serving_candidate_pages=dict(pages),
                      retained_output_directory_bytes=sum(value[0] for value in after_files.values()))
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
        report['status'] = 'passed'
    finally:
        store.conn.close()
        report['sources_after'] = _sources()
        report['sources_unchanged'] = report['sources_after'] == sources
        if not report['sources_unchanged']:
            report['status'] = 'failed_source_guard'
        elif report['status'] != 'passed':
            report['status'] = 'failed_or_incomplete'
        (output / 'candidate-small-update.json').write_text(json.dumps(report, indent=2) + '\n')
        assert report['sources_unchanged'], 'Reviewed recommendation sources changed during acceptance'


def _group_allocation(conn):
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND (name GLOB 'group_*' "
        "OR name GLOB 'unresolved_group_*' OR name IN "
        "('taste_groups','generation_groups','rec_group_clock')) ORDER BY name")]
    objects = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type IN ('table','index') AND tbl_name IN ("
        + ','.join('?' for _ in tables) + ') ORDER BY name', tables)]
    allocated = dict(conn.execute(
        'SELECT name,sum(pgsize) FROM dbstat WHERE name IN ('
        + ','.join('?' for _ in objects) + ') GROUP BY name', objects))
    references = list(group_documents.references(conn))
    files = {filename: Path(filename).stat().st_size
             for filename in {row[3] for row in references}}
    return {
        'rows': {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in tables},
        'objects': {name: allocated.get(name, 0) for name in objects},
        'allocated_bytes': sum(allocated.values()),
        'referenced_files': files, 'referenced_file_bytes': sum(files.values()),
        'referenced_payload_bytes': sum(row[-2] for row in references),
        'database_file_bytes': Path(conn.execute('PRAGMA database_list').fetchone()[2]).stat().st_size,
        'page_count': conn.execute('PRAGMA page_count').fetchone()[0],
        'freelist_pages': conn.execute('PRAGMA freelist_count').fetchone()[0],
    }


def _retained_group_snapshot(store, generation, alias):
    definition = store.taste_groups.definition(alias, generation=generation)
    assert definition is not None, 'The exact historical served alias must remain readable'
    version = store.conn.execute('SELECT group_version_id FROM group_version_aliases '
                                 'WHERE external_version=?', (alias,)).fetchone()[0]
    digest, records = hashlib.sha256(), 0
    for encoded in group_storage.version_rows(store.conn, version):
        digest.update(encoded)
        digest.update(b'\n')
        records += 1
    members = {table: store.conn.execute(
        f'SELECT count(*) FROM {table} WHERE group_version_id=?', (version,)).fetchone()[0]
        for table in ('group_members', 'unresolved_group_members')}
    assert members['group_members'] > 0, 'Retained-version proof needs genuine populated members'
    return {'definition': definition, 'canonical_version_id': version,
            'member_counts': members, 'stream_records': records, 'full_content_sha256': digest.hexdigest()}


def test_one_actual_group_input_update_publishes_and_preserves_retained_version(
        isolated_current_library, monkeypatch):
    db_path, output = isolated_current_library
    sources = _sources()
    assert sources == json.loads((output / 'library-custody.json').read_text())['sources_before']
    report_path = output / 'group-input-continuation.json'
    report = {
        'status': 'incomplete', 'controlled_accepted_feature_change_on_actual_data': True,
        'python': sys.version, 'sqlite': sqlite3.sqlite_version, 'sources_before': sources,
        'max_preparation_turns': 12, 'preparation_wall_allowance_seconds': 180,
        'turns': [], 'publication_unavailable': [],
        'limitations': [
            'Accepted-input consumer boundary; no provider delivery, metadata selection or capture claim',
            'Existing actual musical profile retained; no evidence or user response fabricated',
            'Historical-version check uses a controlled prepared root, not actual offer traffic',
            'Ordinary grouping reads the full admitted corpus; no content/collaborative model fit',
            'Virtual production cadence; warm process/filesystem and observational overhead',
            'Virtual group/storage admissions at 60/300 seconds; leases retain their real wall clock',
            'Group-domain storage helper only; no full ledger-storage worker replay or wall-time latency claim',
            'SQL allocation and referenced-file growth are not physical device-write bytes',
        ],
    }
    store = Store(db_path)
    previous = old_snapshot = selected = None
    try:
        assert Path(sys.modules[group_preparation.__name__].__file__).resolve() == (
            ROOT / 'src/yt_playlist/rec/group_preparation.py')
        store.init_schema()
        conn = store.conn
        previous = store.taste_groups.head()
        assert previous and previous['groups'], 'Use the existing actual published group head'
        report['original_generation'] = previous['generation']
        report['original_fingerprint'] = previous['fingerprint']
        labels = genre_catalog.catalog()[1]
        genres = sorted({genre_catalog.canonical_name(row[0]) for row in conn.execute(
            "SELECT DISTINCT library_genre FROM prepared_recording_inputs WHERE library_present=1 "
            "AND library_genre<>''") if genre_catalog.normalize(
                genre_catalog.canonical_name(row[0])) in labels})
        assert len(genres) > 1, 'A second actual recognized genre is required'
        leaves = sorted((group for group in previous['groups']
                         if group.get('leaf') and not group.get('legacy') and group.get('size', 0) > 0),
                        key=lambda group: (group['size'], group['version']))
        for group in leaves:
            rows = conn.execute(
                'SELECT p.*,r.identity_key,m.payload actual_profile FROM group_version_aliases a '
                'JOIN group_members g ON g.group_version_id=a.group_version_id '
                'JOIN recordings r ON r.id=g.recording_id JOIN prepared_recording_inputs p ON p.recording_id=r.id '
                'JOIN rec_musical_profiles m ON m.identity_key=r.identity_key '
                'WHERE a.external_version=? AND p.library_present=1 AND NOT EXISTS('
                'SELECT 1 FROM recording_redirects d WHERE d.retired_id=r.id) ORDER BY r.id', (group['version'],))
            for row in rows:
                inputs = prepared_inputs.payload(row)
                profile = json.loads(row['actual_profile']) if row['actual_profile'] else None
                original = group_similarity.recording(row['identity_key'], inputs['library'], profile)
                alternatives = sorted((genre for genre in genres if genre != original.genre),
                    key=lambda genre: (genre_map.family(genre) == original.family, genre))
                if not original.genre or not alternatives:
                    continue
                inputs['library']['genre'] = alternatives[0]
                expected = group_versions.recording_inputs([
                    group_similarity.recording(row['identity_key'], inputs['library'], profile)])[row['identity_key']]
                if previous['payload']['recording_inputs'].get(row['identity_key']) == expected:
                    continue
                selected = (dict(row), group, inputs, profile, expected)
                break
            rows.close()
            if selected is not None:
                break
        assert selected is not None, 'No suitable actual retained member and genuine genre change found'
        row, group, inputs, profile, expected = selected
        key, alias = row['identity_key'], group['version']
        raw = json.dumps(inputs, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(raw.encode()).hexdigest()
        assert digest != row['digest']
        old_snapshot = _retained_group_snapshot(store, previous['generation'], alias)
        report.update(changed_recording=key, old_genre=row['library_genre'], new_genre=inputs['library']['genre'],
                      old_input_digest=row['digest'], new_input_digest=digest,
                      expected_group_input_digest=expected, original_retained_version=old_snapshot,
                      actual_profile_sha256=hashlib.sha256(canonical(profile)).hexdigest(),
                      storage_before=_group_allocation(conn))
        queued = list(map(tuple, conn.execute('SELECT * FROM rec_content_pending ORDER BY position,identity_key')))
        counters = store.prepared_content.counters()
        now = time.time()
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            store.taste_groups.retain_evidence('prepared', f'review485-retained:{time.time_ns()}',
                {'group_generation': previous['generation'],
                 'musical_groups': [{'group_id': group['group_id'], 'version': alias}]}, now)
            # None cannot acknowledge any real queue revision. Admit one accepted
            # typed feature change through the production owner transaction.
            store.prepared_content.publish_capture([(key, None, raw, digest, True)], now)
        accepted = conn.execute('SELECT * FROM prepared_recording_inputs WHERE recording_id=?',
                                (row['recording_id'],)).fetchone()
        assert prepared_inputs.payload(accepted) == inputs and accepted['digest'] == digest
        assert accepted['input_revision'] == counters[0] + 1
        assert store.prepared_content.counters() == (counters[0] + 1, counters[1])
        assert list(map(tuple, conn.execute('SELECT * FROM rec_content_pending ORDER BY position,identity_key'))) == queued
        actual_profile = conn.execute('SELECT payload FROM rec_musical_profiles WHERE identity_key=?', (key,)).fetchone()
        assert (actual_profile[0] if actual_profile else None) == row['actual_profile']
        report.update(queued_rows_preserved=len(queued), accepted_input_counter_delta=1, status='preparing')
        report['original_shadow_setting'] = store.settings.get_setting('rec_group_shadow')
        if report['original_shadow_setting'] == '0':
            # Exercise production preparation on this isolated copy even if the
            # retained donor disabled its optional background grouping feature.
            store.settings.set_setting('rec_group_shadow', '1')
            report['controlled_shadow_enable'] = True
        prep = group_preparation.Preparation(store)
        report['production_budget'] = vars(prep.budget)
        original_stage = compact_group_publication.stage
        active_turn = [None]
        def observed_stage(repo, prepared):
            try:
                return original_stage(repo, prepared)
            except group_publication.BuildUnavailable as exc:
                report['publication_unavailable'].append({
                    'turn': active_turn[0], 'wall_at': time.time(),
                    'message': str(exc), 'generation': prepared['generation'],
                    'owner': prepared.get('_stage_owner'),
                    'traceback': [{'file': frame.filename, 'line': frame.lineno, 'function': frame.name}
                                  for frame in traceback.extract_tb(exc.__traceback__)],
                })
                raise
        monkeypatch.setattr(compact_group_publication, 'stage', observed_stage)
        started, first_at, published = time.monotonic(), now, False
        storage_due_at = first_at
        report['storage_interval_seconds'] = 300
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        for index in range(report['max_preparation_turns']):
            remaining = report['preparation_wall_allowance_seconds'] - (time.monotonic() - started)
            if remaining <= 0:
                break
            now = max(now, prep.retry_at)
            active_turn[0] = index + 1
            failures_before = len(report['publication_unavailable'])
            storage_seconds = 0
            storage_admitted = False
            tick = time.monotonic()
            try:
                with work_budget(remaining):
                    if now >= storage_due_at:
                        # Match the evidence-storage worker's 300-second
                        # admission cadence, including inherited prefixes.
                        # The helper keeps its production 0.25-second budget.
                        storage_admitted = True
                        storage_due_at = now + report['storage_interval_seconds']
                        storage_started = time.monotonic()
                        try:
                            group_roots.maintain_retention(store)
                        finally:
                            storage_seconds = time.monotonic() - storage_started
                    published = prep.step(now)
            except WorkBudgetExceeded:
                report['enclosing_wall_allowance_exhausted'] = True
            head = store.taste_groups.head_checkpoint()
            report['turns'].append({'turn': index + 1, 'scheduled_at': now,
                'seconds': time.monotonic() - tick, 'published': published,
                'storage_retention_admitted': storage_admitted,
                'storage_retention_seconds': storage_seconds,
                'publication_unavailable': report['publication_unavailable'][failures_before:],
                'status': group_preparation.status(store), 'has_pending_plan': prep._pending is not None,
                'head': head})
            report.update(preparation_active_seconds=time.monotonic() - started,
                          scheduled_elapsed_seconds=now - first_at)
            report_path.write_text(json.dumps(report, indent=2) + '\n')
            if published:
                break
            assert head and head['generation'] == previous['generation'], 'Deferred work replaced the last-good head'
            assert _retained_group_snapshot(store, previous['generation'], alias) == old_snapshot
        report['storage_after'] = _group_allocation(conn)
        report['sql_row_deltas'] = {table: count - report['storage_before']['rows'][table]
                                   for table, count in report['storage_after']['rows'].items()}
        report['allocated_byte_delta'] = (report['storage_after']['allocated_bytes']
                                         - report['storage_before']['allocated_bytes'])
        report['referenced_file_byte_delta'] = (report['storage_after']['referenced_file_bytes']
                                               - report['storage_before']['referenced_file_bytes'])
        assert _retained_group_snapshot(store, previous['generation'], alias) == old_snapshot
        report['retained_version_unchanged_after_preparation'] = True
        assert list(map(tuple, conn.execute('SELECT * FROM rec_content_pending ORDER BY position,identity_key'))) == queued
        final = store.taste_groups.head()
        if published:
            assert final['generation'] != previous['generation']
            assert final['fingerprint'] != previous['fingerprint']
            assert final['payload']['recording_inputs'][key] == expected
            leaves = store.taste_groups.leaf_members(final['generation'])
            assert final['groups'] and key in leaves
            report.update(new_generation=final['generation'], new_fingerprint=final['fingerprint'],
                          published_groups=len(final['groups']), useful_leaf_members=len(leaves),
                          changed_recording_leaf=leaves[key])
        else:
            report['status'] = 'bounded_defer_incomplete'
            assert final['generation'] == previous['generation']
        store.conn.close()
        store = Store(db_path)
        store.init_schema()
        assert store.taste_groups.head_generation() == final['generation']
        assert _retained_group_snapshot(store, previous['generation'], alias) == old_snapshot
        report['retained_version_unchanged_after_reopen'] = True
        assert list(map(tuple, store.conn.execute(
            'SELECT * FROM rec_content_pending ORDER BY position,identity_key'))) == queued
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
        assert published, 'Grouping deferred within unchanged production budgets; inspect group-input-continuation.json'
        report['status'] = 'passed'
    except Exception as exc:
        report['failure'] = {'type': type(exc).__name__, 'message': str(exc)}
        raise
    finally:
        store.conn.close()
        report['sources_after'] = _sources()
        report['sources_unchanged'] = report['sources_after'] == sources
        if not report['sources_unchanged']:
            report['status'] = 'failed_source_guard'
        elif report['status'] not in ('passed', 'bounded_defer_incomplete'):
            report['status'] = 'failed_or_incomplete'
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        assert report['sources_unchanged'], 'Reviewed group sources changed during acceptance'
