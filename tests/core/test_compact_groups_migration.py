"""Populated group history keeps public aliases while reusing exact member sets."""
import json
from pathlib import Path
import sqlite3
import time

import pytest

from yt_playlist.core import choice_evidence_files, group_documents, group_storage, policy_documents, policy_recovery, sample_files
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.migrations import v070_policy_documents as policies, v071_album_descriptions as descriptions
from yt_playlist.core.migrations import v072_compact_groups as migration
from yt_playlist.core.migrations.runner import Context, MigrationError
from yt_playlist.repos import group_queries, policy_storage


@pytest.fixture
def predecessor(tmp_path):
    conn = sqlite3.connect(tmp_path / 'groups.db')
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    migrate(conn, registry=registry.MIGRATIONS[:69])
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        policies.apply(conn, Context(1))
        descriptions.apply(conn, Context(1))
        conn.executemany('INSERT INTO recordings(id,identity_key) VALUES (?,?)', [(1, 'one'), (2, 'two')])
        conn.executemany('INSERT INTO rec_group_identities VALUES (?,NULL,1)', [('parent',), ('leaf',)])
        for generation, at in [('first', 1), ('second', 2)]:
            inputs = {'one': '1' * 64, 'two': '2' * 64, 'unknown-old-key': '3' * 64}
            payload = json.dumps({'recording_inputs': inputs, 'last_structure_at': 1, 'diagnostics': {'at': at}})
            conn.execute('INSERT INTO rec_group_generations VALUES (?,?,?,?,?,?)',
                         (generation, '{"algorithm":"grouping-1"}', 'input-fingerprint', at, at, payload))
            for group, parent, members in [('parent', None, [('one', .9, .8), ('two', .7, .6)]),
                                            ('leaf', generation + '-parent', [('one', .9, .8)])]:
                version = generation + '-' + group
                definition = json.dumps({'name': group, 'leaf': group == 'leaf', 'legacy': False,
                    'membership_known': True, 'size': len(members), 'depth': int(group == 'leaf'),
                    'rep_keys': ['one'], 'naming_policy': 'naming-1', 'genres': [['House', 1.0]]})
                conn.execute('INSERT INTO rec_group_definitions VALUES (?,?,?,?,?)',
                             (version, generation, group, parent, definition))
                conn.executemany('INSERT INTO rec_group_members VALUES (?,?,?,?)',
                                 [(version, *member) for member in members])
            conn.execute('INSERT INTO rec_group_unknown VALUES (?, ?, ?)', (generation, 'unknown-old-key', 'unmeasured'))
            conn.execute('INSERT INTO rec_group_graph VALUES (?,?,?,?,?)', (generation, 'one', 'two', .8, .7))
            conn.execute('INSERT INTO rec_group_graph VALUES (?,?,?,?,?)', (generation, 'one', 'unknown-old-key', .6, .5))
            conn.execute('INSERT INTO rec_group_assignments VALUES (?,?,?,?,?,?,?)',
                         ('two', generation, generation + '-leaf', .7, .6, 'profile-digest', at))
            if generation == 'second':
                conn.execute('INSERT INTO rec_group_lineage VALUES (?,?,?,?)',
                             ('first-leaf', 'second-leaf', 'continuation', json.dumps(
                                 {'old_share': 1, 'new_share': 1, 'overlap': 1, 'compatible': True})))
            conn.execute('INSERT INTO rec_group_publications VALUES (?)', (generation,))
        conn.execute("INSERT INTO rec_group_head VALUES (1,'second')")
    yield conn
    conn.close()


def apply(conn):
    conn.execute('PRAGMA foreign_keys=OFF')
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            migration.apply(conn, Context(3))
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        conn.execute('PRAGMA foreign_keys=ON')
    migration.validate(conn)


def test_upgrade_deduplicates_content_and_inputs_preserving_every_public_version(predecessor):
    conn = predecessor
    apply(conn)
    assert conn.execute('SELECT count(*) FROM taste_groups').fetchone()[0] == 2
    assert conn.execute('SELECT count(*) FROM group_generations').fetchone()[0] == 2
    assert conn.execute('SELECT count(*) FROM group_versions').fetchone()[0] == 2
    assert conn.execute('SELECT count(*) FROM group_members').fetchone()[0] == 3
    assert conn.execute('SELECT count(*) FROM generation_groups').fetchone()[0] == 4
    assert conn.execute('SELECT count(*) FROM group_input_manifests').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM group_recording_inputs').fetchone()[0] == 2
    assert conn.execute('SELECT count(*) FROM group_manifest_inputs').fetchone()[0] == 2
    assert conn.execute('SELECT count(*) FROM unresolved_group_manifest_inputs').fetchone()[0] == 1
    aliases = dict(conn.execute('SELECT external_version,group_version_id FROM group_version_aliases'))
    assert aliases['first-parent'] == aliases['second-parent']
    assert aliases['first-leaf'] == aliases['second-leaf']
    assert aliases['first-parent'] != aliases['first-leaf']
    for name in ('first', 'second'):
        selected = [tuple(row) for row in conn.execute('SELECT g.external_key,a.external_version '
            'FROM generation_groups x JOIN taste_groups g ON g.id=x.group_id '
            'JOIN group_version_aliases a ON a.external_version=x.external_version '
            'JOIN group_generations n ON n.id=x.generation_id WHERE n.external_key=? ORDER BY x.position', (name,))]
        assert selected == [('parent', name + '-parent'), ('leaf', name + '-leaf')]
    head = conn.execute('SELECT g.external_key,p.published_at FROM group_head h '
                         'JOIN group_generations g ON g.id=h.generation_id '
                         'JOIN group_publications p ON p.generation_id=g.id').fetchone()
    assert tuple(head) == ('second', None)
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_temporal_alias_lineage_survives_reused_source_and_target_content(predecessor):
    conn = predecessor
    apply(conn)
    row = conn.execute('SELECT * FROM group_lineage').fetchone()
    assert (row['source_alias'], row['target_alias'], row['relation'], row['compatible']) == (
        'first-leaf', 'second-leaf', 'continuation', 1)
    assert row['source_version_id'] == row['target_version_id']
    assert group_documents.read(row['evidence_filename'], row['evidence_digest'],
        row['evidence_bytes'], row['evidence_offset'], 'lineage') == {
            'old_share': 1, 'new_share': 1, 'overlap': 1, 'compatible': True}
    assert conn.execute('SELECT count(*) FROM group_graph').fetchone()[0] == 2
    assert conn.execute('SELECT count(*) FROM unresolved_group_graph').fetchone()[0] == 2
    assert [tuple(row) for row in conn.execute('SELECT raw_key,reason FROM unresolved_group_unknown')] == [
        ('unknown-old-key', 'unmeasured'), ('unknown-old-key', 'unmeasured')]
    assert conn.execute('SELECT count(*) FROM group_assignments').fetchone()[0] == 2


def test_unfinished_original_member_prefix_keeps_its_lease_and_cannot_become_history(predecessor):
    conn = predecessor
    expires = time.time() + 3600
    with conn:
        conn.execute("INSERT INTO rec_group_identities VALUES ('private',NULL,3)")
        conn.execute('INSERT INTO rec_group_generations VALUES (?,?,?,?,?,?)',
                     ('unfinished', '{"algorithm":"grouping-1"}', 'unfinished-input', 3, 3,
                      '{"recording_inputs":{"one":"' + '1' * 64 + '"}}'))
        conn.execute('INSERT INTO rec_group_builds VALUES (?,?,?,0)', ('unfinished', 'original-worker', expires))
        conn.execute('INSERT INTO rec_group_definitions VALUES (?,?,?,?,?)',
                     ('private-prefix', 'unfinished', 'private', None, json.dumps({
                         'name': 'Private', 'membership_known': True, 'size': 2, 'leaf': True,
                         'legacy': False, 'depth': 0, 'rep_keys': ['one']})))
        conn.execute("INSERT INTO rec_group_members VALUES ('private-prefix','one',.8,.7)")
    apply(conn)
    build = conn.execute('SELECT b.*,g.metadata_filename,g.metadata_digest,g.metadata_bytes,g.metadata_offset '
                         "FROM group_builds b JOIN group_generations g ON g.id=b.generation_id WHERE g.external_key='unfinished'").fetchone()
    assert (build['owner'], build['expires_at'], build['ready']) == ('cleanup', expires, 0)
    metadata = group_documents.read(build['metadata_filename'], build['metadata_digest'],
                                     build['metadata_bytes'], build['metadata_offset'], 'generation')
    assert metadata['_legacy_build'] == {'owner': 'original-worker', 'expires_at': expires, 'ready': 0}
    prefix = conn.execute('SELECT b.*,v.size FROM group_build_versions b JOIN group_versions v '
                           'ON v.id=b.group_version_id').fetchone()
    assert (prefix['external_version'], prefix['size']) == ('private-prefix', 2)
    assert conn.execute('SELECT count(*) FROM group_members WHERE group_version_id=?',
                         (prefix['group_version_id'],)).fetchone()[0] == 1
    assert group_queries.definition(conn, 'private-prefix') is None
    assert group_queries.head_generation(conn) == 'second'
    with pytest.raises(sqlite3.IntegrityError, match='incomplete membership'), conn:
        conn.execute('INSERT INTO group_version_aliases VALUES (?,?,?)',
                     ('private-prefix', prefix['group_version_id'], build['generation_id']))
    with pytest.raises(sqlite3.IntegrityError, match='publication is incomplete'), conn:
        conn.execute('INSERT INTO group_publications VALUES (?,3)', (build['generation_id'],))


def test_compact_readers_reconstruct_exact_generation_context_and_aliases(predecessor):
    conn = predecessor
    apply(conn)
    assert group_queries.head_generation(conn) == 'second'
    assert group_queries.head_checkpoint(conn) == {'generation': 'second', 'serial': 2,
                                                   'policy': '{"algorithm":"grouping-1"}'}
    head = group_queries.head(conn, members=True)
    assert head['generation'] == 'second' and head['created_at'] == 2
    assert head['payload']['recording_inputs'] == {
        'one': '1' * 64, 'two': '2' * 64, 'unknown-old-key': '3' * 64}
    assert head['payload']['diagnostics'] == {'at': 2}
    assert head['groups'][0]['members'] == {'one': (.9, .8), 'two': (.7, .6)}
    assert head['groups'][1]['members'] == {'one': (.9, .8)}
    for generation in ('first', 'second'):
        leaf = group_queries.definition(conn, generation + '-leaf', generation=generation)
        assert leaf['version'] == generation + '-leaf'
        assert leaf['generation'] == generation
        assert leaf['parent_version'] == generation + '-parent'
        assert group_queries.leaf_members(conn, generation) == {'one': generation + '-leaf'}
    assert group_queries.definition(conn, 'first-leaf', generation='second') is None
    assert group_queries.definition(conn, 'absent') is None
    assert group_queries.canonical_version(conn, 'first-leaf') == group_queries.canonical_version(conn, 'second-leaf')
    assert group_queries.assignment_head(conn)['groups'] == [{'version': 'second-leaf', 'rep_keys': ['one']}]
    assert group_queries.successors(conn, 'first-leaf') == [{
        'source': 'first-leaf', 'target': 'second-leaf', 'relation': 'continuation',
        'payload': {'old_share': 1, 'new_share': 1, 'overlap': 1, 'compatible': True}}]


def test_repeated_unchanged_import_adds_only_publication_mappings(predecessor):
    conn = predecessor
    prepared = migration._prepared(conn, conn.execute(
        "SELECT * FROM rec_group_generations WHERE generation='second'").fetchone())
    apply(conn)
    original_members = [tuple(row) for row in conn.execute('SELECT * FROM group_members')]
    original_files = set(group_documents.directory(conn).glob('*/*.json'))
    prepared['generation'] = 'third'
    for group in prepared['groups']:
        group['version'] = group['version'].replace('second-', 'third-')
        if group['parent_version']:
            group['parent_version'] = group['parent_version'].replace('second-', 'third-')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("INSERT INTO group_generations(external_key,policy_version,input_fingerprint,input_manifest_id,"
                     'input_serial,created_at,metadata_filename,metadata_digest,metadata_bytes,metadata_offset) '
                     "SELECT 'third',policy_version,input_fingerprint,input_manifest_id,3,3,metadata_filename,"
                     "metadata_digest,metadata_bytes,metadata_offset FROM group_generations WHERE external_key='second'")
        generation_id = conn.execute("SELECT id FROM group_generations WHERE external_key='third'").fetchone()[0]
        group_storage.install_versions(conn, prepared, generation_id, {'one': 1, 'two': 2}.get)
    assert conn.execute('SELECT count(*) FROM group_versions').fetchone()[0] == 2
    assert conn.execute('SELECT count(*) FROM generation_groups').fetchone()[0] == 6
    assert [tuple(row) for row in conn.execute('SELECT * FROM group_members')] == original_members
    assert set(group_documents.directory(conn).glob('*/*.json')) == original_files


@pytest.mark.parametrize('problem', ['bad_json', 'nonfinite_score', 'input_digest', 'parent_cycle',
                                     'assignment_generation', 'wrong_size', 'unknown_members', 'legacy_members'])
def test_invalid_original_state_is_rejected_before_any_file_or_owner_is_replaced(predecessor, problem):
    conn = predecessor
    # Controlled corrupt-predecessor fixture; product writers remain immutable.
    for table in ('rec_group_generations', 'rec_group_definitions', 'rec_group_members'):
        conn.execute(f'DROP TRIGGER immutable_{table}_update')
    with conn:
        if problem == 'bad_json':
            conn.execute("UPDATE rec_group_definitions SET payload='invalid original' WHERE version='second-leaf'")
        elif problem == 'nonfinite_score':
            conn.execute('PRAGMA ignore_check_constraints=ON')
            conn.execute("UPDATE rec_group_members SET similarity=1e999 WHERE version='second-leaf'")
            conn.execute('PRAGMA ignore_check_constraints=OFF')
        elif problem == 'input_digest':
            conn.execute("UPDATE rec_group_generations SET payload=? WHERE generation='second'",
                         ('{"recording_inputs":{"one":"unknown"}}',))
        elif problem == 'parent_cycle':
            conn.execute("UPDATE rec_group_definitions SET parent_version='second-leaf' WHERE version='second-parent'")
        elif problem in ('wrong_size', 'unknown_members', 'legacy_members'):
            value = json.loads(conn.execute("SELECT payload FROM rec_group_definitions WHERE version='second-leaf'").fetchone()[0])
            value.update({'size': 9} if problem == 'wrong_size' else
                         {'membership_known': False} if problem == 'unknown_members' else {'legacy': True})
            conn.execute("UPDATE rec_group_definitions SET payload=? WHERE version='second-leaf'", (json.dumps(value),))
        else:
            conn.execute("UPDATE rec_group_assignments SET version='first-leaf' WHERE generation='second'")
    original = list(conn.iterdump())
    with pytest.raises(MigrationError):
        apply(conn)
    assert list(conn.iterdump()) == original
    assert not group_documents.directory(conn).exists()


def test_interrupted_group_relocation_preserves_original_history_and_can_restart(predecessor, monkeypatch):
    conn = predecessor
    original = list(conn.iterdump())
    publish = policy_documents._publish_encoded
    calls = 0
    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError('controlled group relocation interruption')
        return publish(*args, **kwargs)
    monkeypatch.setattr(policy_documents, '_publish_encoded', interrupted)
    with pytest.raises(OSError, match='controlled group relocation interruption'):
        apply(conn)
    assert list(conn.iterdump()) == original
    monkeypatch.setattr(policy_documents, '_publish_encoded', publish)
    apply(conn)
    assert conn.execute('SELECT count(*) FROM group_members').fetchone()[0] == 3
    for row in conn.execute('SELECT * FROM group_versions'):
        assert group_documents.read(row['definition_filename'], row['definition_digest'],
            row['definition_bytes'], row['definition_offset'], 'definition')['name'] in ('parent', 'leaf')


def test_database_rejects_wrong_version_owner_and_content_mutation(predecessor):
    conn = predecessor
    apply(conn)
    for sql in ('UPDATE group_members SET similarity=.1',
                'DELETE FROM group_members',
                'INSERT INTO group_members SELECT group_version_id,2,.2,.2,NULL FROM group_members WHERE recording_id=1 '
                'AND group_version_id=(SELECT max(id) FROM group_versions)',
                'UPDATE group_version_aliases SET group_version_id=999',
                "UPDATE group_recording_inputs SET input_digest='0'",
                'UPDATE generation_groups SET group_version_id=(SELECT max(id) FROM group_versions) '
                'WHERE group_id=(SELECT min(id) FROM taste_groups)'):
        with pytest.raises(sqlite3.IntegrityError), conn:
            conn.execute(sql)
    plan = ' '.join(row[3] for row in conn.execute('EXPLAIN QUERY PLAN SELECT group_version_id '
                                                  'FROM group_members WHERE recording_id=1'))
    assert 'ix_group_member_recording' in plan and 'SCAN ' not in plan


def test_group_file_collection_respects_reader_lifetime_and_exact_owners(predecessor):
    conn = predecessor
    apply(conn)
    filename, _digest, _size, _offset = next(group_documents.publish_many(
        group_documents.directory(conn), [('generation', {'uncommitted': 'orphan'})]))
    with policy_documents.retained_reader(group_documents.directory(conn)):
        assert group_documents.collect_orphans(conn) == 0
        assert Path(filename).exists()
    assert group_documents.collect_orphans(conn) == 1
    assert not Path(filename).exists()
    for _table, _key, kind, filename, digest, size, offset in group_documents.references(conn):
        group_documents.read(filename, digest, size, offset, kind)


def test_group_backup_restores_all_owned_files_after_originals_are_lost(predecessor, tmp_path):
    conn = predecessor
    apply(conn)
    original = {(table, key): (kind, digest, size, group_documents.read(filename, digest, size, offset, kind))
                for table, key, kind, filename, digest, size, offset in group_documents.references(conn)}
    with policy_documents.retained_reader(group_documents.directory(conn)), conn:
        conn.execute('BEGIN')
        manifest = group_documents.export(conn, tmp_path / 'group-backup')
    for path in set(filename for _table, _key, _kind, filename, _digest, _size, _offset
                    in group_documents.references(conn)):
        Path(path).unlink()
    group_documents.restore(conn, manifest)
    restored = {(table, key): (kind, digest, size, group_documents.read(filename, digest, size, offset, kind))
                for table, key, kind, filename, digest, size, offset in group_documents.references(conn)}
    assert restored == original
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    before = list(conn.iterdump())
    migration.validate(conn)
    group_documents.restore(conn, manifest)
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize('kind', ['compact', 'verified-backup'])
def test_populated_sample_and_choice_owners_recover_without_producer(tmp_path, kind):
    from contextlib import closing
    from tests.test_audio_vibes import sample
    from yt_playlist.core.store import Store
    from yt_playlist.rec import choice_evidence

    source_path = tmp_path / 'producer' / 'state.db'
    source_path.parent.mkdir()
    source = Store(source_path)
    try:
        source.init_schema()
        samples = [sample(run_id='recovery', point=point) for point in ('thirty', 'middle', 'two_thirds')]
        for index, value in enumerate(samples):
            source.audio_vibes.save_sample(value, 100 + index)
        opportunity = choice_evidence.issue(source, 'mixes', [
            {'id': 'close', 'label': 'Stay close', 'genres': ['Deep House']},
            {'id': 'explore', 'label': 'Explore', 'genres': ['Jazz']}],
            100, context={'at': 100}, opportunity_id='recovery-menu')
        assert opportunity == 'recovery-menu'
        choice_evidence.respond(source, opportunity, 'visible', 101, visible=['close', 'explore'])
        choice_evidence.respond(source, opportunity, 'choice', 102, option_id='close')
        expected_report = choice_evidence.compile_report(source, 110)['formats']
        samples_root = sample_files.directory(source.conn)
        choices_root = choice_evidence_files.directory(source.conn)

        def sample_bytes(conn):
            return {identity: sample_files._encoded(filename)
                    for identity, filename in conn.execute('SELECT id,filename FROM sample_attributes')}

        def choice_bytes(conn):
            root = choice_evidence_files.directory(conn)
            return {str(path.relative_to(root)): choice_evidence_files._read(path)
                    for path in choice_evidence_files.references(conn)}

        expected_samples, expected_choices = sample_bytes(source.conn), choice_bytes(source.conn)
        assert len(expected_samples) == 3 and expected_choices
        source_sql = list(source.conn.iterdump())
        report = policy_recovery.recover(source_path, tmp_path / 'recovered', progress=lambda _: None)
        candidate_path = Path(report['compact_database'])
        if kind == 'verified-backup':
            candidate_path = tmp_path / 'independent' / 'state.db'
            candidate_path.parent.mkdir()
            with closing(sqlite3.connect(report['verified_backup'])) as backup, \
                    closing(sqlite3.connect(candidate_path)) as target:
                backup.backup(target)
                target.row_factory = sqlite3.Row
                policy_recovery._restore_files(target, report['verified_backup_file_manifests'])
        hidden_samples, hidden_choices = samples_root.with_name('hidden-samples'), choices_root.with_name('hidden-choices')
        samples_root.rename(hidden_samples)
        choices_root.rename(hidden_choices)
        try:
            recovered = Store(candidate_path)
            try:
                recovered.init_schema(defer_metadata=True)
                assert recovered.audio_vibes.samples('recovery') == samples
                assert sample_bytes(recovered.conn) == expected_samples
                assert choice_bytes(recovered.conn) == expected_choices
                assert choice_evidence.compile_report(recovered, 110)['formats'] == expected_report
                assert not recovered.conn.execute('PRAGMA foreign_key_check').fetchall()
            finally:
                recovered.conn.close()
        finally:
            hidden_samples.rename(samples_root)
            hidden_choices.rename(choices_root)
        assert list(source.conn.iterdump()) == source_sql
        assert sample_bytes(source.conn) == expected_samples
        assert choice_bytes(source.conn) == expected_choices
    finally:
        source.conn.close()


def test_interrupted_group_restore_leaves_every_database_reference_unchanged(predecessor, tmp_path, monkeypatch):
    conn = predecessor
    apply(conn)
    with policy_documents.retained_reader(group_documents.directory(conn)), conn:
        conn.execute('BEGIN')
        manifest = group_documents.export(conn, tmp_path / 'group-backup')
    original = list(conn.iterdump())
    publish = policy_documents._publish_encoded
    calls = 0
    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('controlled restore interruption')
        return publish(*args, **kwargs)
    monkeypatch.setattr(group_documents, 'PACK_BYTES', 1)
    monkeypatch.setattr(policy_documents, '_publish_encoded', interrupted)
    with pytest.raises(OSError, match='controlled restore interruption'):
        group_documents.restore(conn, manifest)
    assert list(conn.iterdump()) == original
    monkeypatch.setattr(policy_documents, '_publish_encoded', publish)
    group_documents.restore(conn, manifest)
    for _table, _key, kind, filename, digest, size, offset in group_documents.references(conn):
        group_documents.read(filename, digest, size, offset, kind)


@pytest.mark.parametrize('problem', ['key_type', 'missing_owner', 'duplicate', 'offset', 'path', 'digest'])
def test_group_backup_rejects_wrong_owner_sets_and_file_ranges(predecessor, tmp_path, problem):
    conn = predecessor
    apply(conn)
    root = tmp_path / 'group-backup'
    with policy_documents.retained_reader(group_documents.directory(conn)), conn:
        conn.execute('BEGIN')
        manifest = group_documents.export(conn, root)
    document = group_documents._read_manifest(manifest)
    digest = document['chunks'][0]
    chunk = group_documents._read_manifest(root / digest[:2] / (digest + '.json'))
    entry = chunk['records'][0]
    if problem == 'key_type':
        entry['key'] = [True]
    elif problem == 'missing_owner':
        chunk['records'].pop()
    elif problem == 'duplicate':
        chunk['records'].append(dict(entry))
    elif problem == 'offset':
        entry['offset'] += 1
    elif problem == 'path':
        entry['relative_filename'] = '../outside.json'
    else:
        entry['digest'] = '0' * 64
    document['chunks'][0] = Path(group_documents._manifest(root, chunk)).stem
    bad_manifest = group_documents._manifest(root, document)
    original = list(conn.iterdump())
    with pytest.raises(ValueError):
        group_documents.restore(conn, bad_manifest)
    assert list(conn.iterdump()) == original


def test_recovery_bundle_preserves_group_and_policy_owners_together(predecessor, tmp_path):
    conn = predecessor
    apply(conn)
    members = {version: list(group_storage.version_rows(conn, version))
               for version, in conn.execute('SELECT id FROM group_versions')}
    configs = {version: policy_storage.configuration(conn, version)
               for version, in conn.execute('SELECT version FROM recommendation_policies')}
    assert members and all(members.values()) and configs
    links = {table: list(map(tuple, conn.execute('SELECT id,policy_version FROM '+table+' ORDER BY id')))
             for table in ('group_versions', 'group_generations')}
    restored = sqlite3.connect(tmp_path / 'restored.db')
    restored.row_factory = sqlite3.Row
    try:
        with policy_documents.retained_reader(policy_documents.directory(conn)), \
                policy_documents.retained_reader(group_documents.directory(conn)), conn:
            conn.execute('BEGIN')
            manifests = policy_recovery._export_files(conn, tmp_path / 'restored.db', backup_root=tmp_path / 'backup')
            conn.backup(restored)
        assert set(manifests) == {'annotations', 'policies', 'descriptions', 'groups', 'samples', 'choice_evidence'}
        # This predecessor has no samples or choice evidence. Populated custody
        # is covered by their separate recovery tests, rather than inferred here.
        assert list(sample_files._entries(manifests['samples'])) == []
        assert json.loads(choice_evidence_files._read(Path(manifests['choice_evidence']))) == {
            'format': 'choice-evidence-1', 'pages': []}
        for root in (group_documents.directory(conn), policy_documents.directory(conn)):
            for path in root.glob('*/*.json'):
                path.unlink()
        restored.execute('PRAGMA foreign_keys=ON')
        policy_recovery._restore_files(restored, manifests)
        migration.validate(restored)
        assert {version: list(group_storage.version_rows(restored, version)) for version in members} == members
        assert {version: policy_storage.configuration(restored, version) for version in configs} == configs
        assert {table: list(map(tuple, restored.execute('SELECT id,policy_version FROM '+table+' ORDER BY id')))
                for table in links} == links
        assert not restored.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        restored.close()
