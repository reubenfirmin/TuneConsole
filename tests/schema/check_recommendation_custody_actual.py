"""Compare recommendation evidence roots and published outputs on a managed upgrade."""
from collections import Counter
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
from time import perf_counter

import pytest
from fastapi.testclient import TestClient

from tests.library_image.fixtures import report_file
from tests.library_image.file_references import installed_policy_history
from yt_playlist import __main__ as startup
from yt_playlist.core import (
    file_map, group_evidence, policy_bodies, policy_documents, policy_history_read,
    recommendation_chunks, recommendation_custody, recommendation_outputs,
)
from yt_playlist.core.migrations import (
    policy_body_settings, policy_history_settings, recommendation_chunk_receipts as chunk_migration, regular,
)
from yt_playlist.core.policy_config import BASE_FIELD
from yt_playlist.core.policy_paths import record_path
from yt_playlist.core.store import Store


def _installed_policy(conn, version):
    row = conn.execute('SELECT configuration_filename,content_digest,configuration_offset,byte_size,base_version '
                       'FROM recommendation_policies WHERE version=?', (version,)).fetchone()
    variant = policy_documents.read(row[0], row[1], offset=row[2], size=row[3])
    base = variant.pop(BASE_FIELD, None)
    assert base == row[4]
    if base is None:
        return variant
    body = conn.execute('SELECT configuration_filename,content_digest,configuration_offset,byte_size '
                        'FROM recommendation_policy_bodies WHERE version=?', (base,)).fetchone()
    return {**policy_documents.read(body[0], body[1], offset=body[2], size=body[3]), **variant}


def test_shared_policy_settings_preserve_installed_bodies(managed_library):
    store = Store(managed_library)
    with closing(store.conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:223])
        table_count = "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'"
        before = conn.execute(table_count).fetchone()[0]
        select_bodies = f'SELECT version,{",".join(policy_bodies.COLUMNS)},served FROM {{owner}} ORDER BY version'
        bodies = list(map(tuple, conn.execute(select_bodies.format(owner=policy_body_settings.RETIRED))))
        assert bodies
        variants = list(map(tuple, conn.execute('SELECT * FROM recommendation_policies ORDER BY version')))
        references = list(conn.execute('SELECT base_version,min(version),count(*) FROM recommendation_policies '
                                       'WHERE base_version IS NOT NULL GROUP BY base_version'))
        shared = {version: _installed_policy(conn, version) for _base, version, _count in references}
        assert max(row[2] for row in references) > 1
        body_values = {version: policy_documents.read(filename, digest, offset=offset, size=size)
                       for version, filename, digest, size, offset, _created, _served in bodies}

        regular.migrate(conn, changes=regular.CHANGES[:224])
        store._bind_repositories()
        assert conn.execute(table_count).fetchone()[0] == before - 1
        assert not conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (policy_body_settings.RETIRED,)).fetchone()
        assert list(map(tuple, conn.execute(select_bodies.format(owner=policy_bodies.VIEW)))) == bodies
        assert list(map(tuple, conn.execute('SELECT * FROM recommendation_policies ORDER BY version'))) == variants
        for version, expected in shared.items():
            assert store.rec_ledger.policy_configuration(version) == expected
        for version, expected in body_values.items():
            body = policy_bodies.read(conn, version)
            assert policy_documents.read(body['configuration_filename'], body['content_digest'],
                                         offset=body['configuration_offset'], size=body['byte_size']) == expected
        with pytest.raises(sqlite3.IntegrityError, match='body is in use'), conn.atomic():
            policy_bodies.delete(conn, [references[0][0]])
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        report_file('shared-policy-settings-c224.json').write_text(json.dumps({
            'tables_before': before, 'tables_after': before - 1, 'bodies': len(bodies), 'variants': len(variants),
            'variants_with_a_body': sum(row[2] for row in references), 'sampled_variants': len(shared),
            'max_variants_sharing_one_body': max(row[2] for row in references),
        }, indent=2) + '\n')


def _installed_archive(conn, version):
    state, identity, digest, size, head = conn.execute('SELECT state,archive_file_id,archive_digest,'
        'archive_byte_size,shared_archive_head FROM recommendation_policy_history WHERE version=?', (version,)).fetchone()
    saved = {'state': state, 'publication': head,
             'file': {'id': identity, 'content_digest': digest, 'byte_size': size} if identity is not None else None}
    return policy_history_read.read(policy_history_read.directory(conn), version, saved)


def test_archive_settings_preserve_installed_impression_attribution(managed_library):
    store = Store(managed_library)
    with closing(store.conn) as conn:
        history = policy_history_read
        regular.migrate(conn, changes=regular.CHANGES[:224])
        table_count = "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'"
        before = conn.execute(table_count).fetchone()[0]
        select = f'SELECT version,{",".join(history.FIELDS)} FROM {{owner}} ORDER BY version'
        entries = list(map(tuple, conn.execute(select.format(owner=policy_history_settings.RETIRED))))
        impressions = list(map(tuple, conn.execute('SELECT * FROM rec_served_impressions ORDER BY id')))
        assert entries and impressions
        archive_samples = [entry[0] for entry in entries[::max(1, len(entries) // 24)]]
        expected = {version: _installed_archive(conn, version) for version in archive_samples}
        roots = installed_policy_history(conn)
        archive_serial = conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?',
                                      (policy_history_settings.RETIRED,)).fetchone()[0]
        impression_serial = conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?',
                                         (policy_history_settings.IMPRESSIONS,)).fetchone()[0]
        impression_storage = "SELECT name,rootpage FROM sqlite_schema WHERE tbl_name=? AND rootpage>0 ORDER BY name"
        impression_pages = list(conn.execute(impression_storage, (policy_history_settings.IMPRESSIONS,)))

        cutover = perf_counter()
        regular.migrate(conn, changes=regular.CHANGES[:225])
        elapsed = perf_counter() - cutover
        store._bind_repositories()
        assert conn.execute(table_count).fetchone()[0] == before - 1
        assert not conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (policy_history_settings.RETIRED,)).fetchone()
        assert list(map(tuple, conn.execute(select.format(owner=history.VIEW)))) == entries
        assert list(map(tuple, conn.execute('SELECT * FROM rec_served_impressions ORDER BY id'))) == impressions
        assert (history.publication(conn), sorted(history.archive_ids(conn))) == (roots[0], sorted(roots[1]))
        assert {version: history.document(conn, version) for version in archive_samples} == expected
        assert int(conn.execute('SELECT value FROM settings WHERE key=?',
                                (history.NEXT_ARCHIVE_FILE,)).fetchone()[0]) >= archive_serial
        assert conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?',
                            (policy_history_settings.IMPRESSIONS,)).fetchone()[0] == impression_serial
        assert list(conn.execute(impression_storage, (policy_history_settings.IMPRESSIONS,))) == impression_pages
        linked = conn.execute('SELECT primary_historical_version FROM rec_served_impressions '
                              'WHERE primary_historical_version IS NOT NULL LIMIT 1').fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match='attribution retains'), conn.atomic():
            conn.execute('DELETE FROM settings WHERE key=?', (history.PREFIX + linked,))
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        report_file('policy-archive-settings-c225.json').write_text(json.dumps({
            'tables_before': before, 'tables_after': before - 1, 'history_entries': len(entries),
            'impressions': len(impressions), 'archive_configurations_read': len(expected), 'elapsed': elapsed,
        }, indent=2) + '\n')


def test_recommendation_root_owners_survive_the_upgrade(managed_library):
    store = Store(managed_library)
    with closing(store.conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:201])
        old = list(map(tuple, conn.execute('SELECT kind,external_key,generation_id '
                                          'FROM recommendation_generation_refs ORDER BY 1,2,3')))
        assert old
        outputs = list(recommendation_outputs.references(conn))
        selected = list(map(tuple, conn.execute('SELECT generation_id,surface FROM '
                                               'recommendation_generation_surfaces WHERE selected=1 ORDER BY 1,2')))
        assert selected
        versions = [row[0] for row in conn.execute('SELECT DISTINCT g.policy_version FROM recommendation_generations g '
            'JOIN recommendation_generation_surfaces s ON s.generation_id=g.id WHERE s.selected=1 ORDER BY 1')]
        policies = {version: _installed_policy(conn, version) for version in versions}
        root = policy_history_read.directory(conn)
        head, identities = installed_policy_history(conn)
        history = sorted(map(str, [*file_map.pages(root, head), *(record_path(root, identity) for identity in identities)]))
        archived = conn.execute("SELECT version FROM recommendation_policy_history WHERE state='archived' LIMIT 1").fetchone()
        archived_document = _installed_archive(conn, archived[0]) if archived else None
        regular.migrate(conn, changes=regular.CHANGES[:212])
        contexts = list(map(tuple, conn.execute('SELECT * FROM group_evidence_owners ORDER BY kind,external_key')))
        references = list(map(tuple, conn.execute('SELECT kind,external_key,reference_key,reference_kind,group_version_id,reason '
                                                  'FROM group_evidence_references ORDER BY 1,2,3,4')))
        table_count = conn.execute("SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'").fetchone()[0]
        regular.migrate(conn, changes=regular.CHANGES[:213])
        destination_count = conn.execute("SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'").fetchone()[0]
        assert destination_count == table_count - 1
        regular.migrate(conn, changes=regular.CHANGES[:225])
        group_settings_before = conn.execute("SELECT count(*) FROM sqlite_schema WHERE type='table' "
                                              "AND name NOT GLOB 'sqlite_*'").fetchone()[0]
        regular.migrate(conn, changes=regular.CHANGES[:226])
        group_settings_after = conn.execute("SELECT count(*) FROM sqlite_schema WHERE type='table' "
                                             "AND name NOT GLOB 'sqlite_*'").fetchone()[0]
        assert group_settings_after == group_settings_before - 1
        store.init_schema()
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='recommendation_generation_refs'").fetchone()
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='group_evidence_owners'").fetchone()
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='group_evidence_references'").fetchone()
        assert list(recommendation_outputs.references(conn)) == outputs

    store = Store(managed_library)
    with closing(store.conn) as conn:
        store.init_schema()
        roots = ('SELECT root.value->>0,root.value->>1,g.id FROM ' + recommendation_custody.ROOTS +
                 ' UNION SELECT home.value->>1,home.value->>2,home.value->>3 FROM ' +
                 recommendation_custody.HOME_ROOTS + ' ORDER BY 1,2,3')
        assert list(map(tuple, conn.execute(roots))) == old
        duplicate_roots = conn.execute(f'''SELECT count(*) FROM {recommendation_custody.ROOTS}
            AND EXISTS(SELECT 1 FROM {recommendation_custody.HOME_ROOTS}
            AND home.value->>3=g.id AND root.value=json_array(home.value->>1,home.value->>2))''').fetchone()[0]
        assert duplicate_roots == 0
        assert list(map(tuple, conn.execute('SELECT generation_id,surface FROM '
                                           'recommendation_generation_surfaces WHERE selected=1 ORDER BY 1,2'))) == selected
        assert {version: policy_documents.configuration(conn, version) for version in versions} == policies
        assert sorted(map(str, policy_history_read.references(conn))) == history
        if archived:
            assert policy_history_read.document(conn, archived[0]) == archived_document
        home = conn.execute("SELECT r.value->>1,r.value->>2 FROM prepared_display_manifests s,"
                            "json_each(s.value,'$.recommendation_roots') r "
                            "WHERE s.key GLOB 'prepared_home:*' LIMIT 1").fetchone()
        if home:
            with pytest.raises(sqlite3.IntegrityError, match='Home display retains this reference'), conn.atomic():
                recommendation_custody.release(conn, *home)
            [generation, *_] = recommendation_custody.retainers(conn, *home)
            with pytest.raises(sqlite3.IntegrityError, match='Home display retains this reference'), conn.atomic():
                conn.execute("UPDATE recommendation_generations SET status='retiring' WHERE id=?", (generation,))
        assert list(map(tuple, conn.execute(f'SELECT * FROM {group_evidence.CONTEXTS} ORDER BY kind,external_key'))) == contexts
        home_references = group_evidence.HOME_CONTEXTS.replace(' WHERE ', ",json_each(context.value,'$[8]') r WHERE ")
        assert list(map(tuple, conn.execute('SELECT kind,external_key,reference_key,reference_kind,group_version_id,reason '
                f'FROM {group_evidence.REFERENCES} UNION ALL '
                f'SELECT context.value->>1,context.value->>2,r.value->>0,r.value->>1,r.value->>2,r.value->>3 FROM {home_references} '
                'ORDER BY 1,2,3,4'))) == references
        home_contexts = conn.execute(f'SELECT count(*) FROM {group_evidence.HOME_CONTEXTS}').fetchone()[0]
        assert not conn.execute(f'''SELECT 1 FROM {group_evidence.HOME_CONTEXTS}
            AND EXISTS(SELECT 1 FROM settings
                WHERE key='{group_evidence.PREFIX}'||(context.value->>1)||':'||(context.value->>2))''').fetchone()
        settings_rows = list(map(tuple, conn.execute(group_evidence.setting_contexts())))
        generation_holds = Counter(context[-1] for context in settings_rows if context[-1] is not None)
        distinct_contexts = {(kind, identity, version) for kind, identity, version in conn.execute(
            f'SELECT kind,external_key,group_version_id FROM {group_evidence.REFERENCES} WHERE group_version_id IS NOT NULL')}
        version_holds = Counter(version for _, _, version in distinct_contexts)
        for table, holds in (('group_generations', generation_holds), ('group_versions', version_holds)):
            assert dict(conn.execute(f'SELECT id,evidence_holds FROM {table} WHERE evidence_holds>0')) == holds
        home_group = conn.execute(f'SELECT context.value->>7 FROM {group_evidence.HOME_CONTEXTS} '
                                  'AND context.value->>7 IS NOT NULL LIMIT 1').fetchone()
        if home_group:
            with pytest.raises(sqlite3.IntegrityError, match='Home display retains this group generation'), conn.atomic():
                conn.execute('DELETE FROM group_generations WHERE id=?', home_group)
        home_version = conn.execute(f'SELECT reference.value->>2 FROM {group_evidence.HOME_VERSIONS} LIMIT 1').fetchone()
        if home_version:
            with pytest.raises(sqlite3.IntegrityError, match='Home display retains this group version'), conn.atomic():
                conn.execute('DELETE FROM group_versions WHERE id=?', home_version)
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    report_file('recommendation-custody-c213.json').write_text(json.dumps({
        'evidence_roots': len(old), 'selected_generations': len(selected), 'file_references': len(outputs),
        'duplicate_home_roots': duplicate_roots,
        'retained_home_root_checked': home is not None, 'live_policy_versions_checked': len(policies),
        'policy_archive_file_references': len(history), 'archived_policy_replayed': archived is not None,
        'group_contexts': len(contexts), 'group_references': len(references), 'home_group_contexts': home_contexts,
        'home_group_generation_guard_checked': home_group is not None, 'home_group_version_guard_checked': home_version is not None,
        'source_table_count': table_count, 'destination_table_count': destination_count,
        'group_settings_count_before': group_settings_before, 'group_settings_count_after': group_settings_after,
        'group_settings_contexts': len(settings_rows),
    }, indent=2) + '\n', encoding='utf-8')


def test_prepared_chunk_receipts_keep_directory_and_indexed_ranks(managed_library, monkeypatch, tmp_path):
    def rank_query(relation):
        return (f'SELECT id,last_rank FROM {relation} WHERE generation_id=? AND surface=? AND pool_id=? '
                'AND first_rank<=? ORDER BY first_rank DESC LIMIT 1')

    def rank_owner(conn, query, coordinates):
        row = conn.execute(query, coordinates).fetchone()
        return row['id'] if row and row['last_rank'] >= coordinates[-1] else None

    count_sql = "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'"
    copy = chunk_migration._copy_page
    batches = []
    monkeypatch.setattr(startup.paths, 'db_path', lambda: managed_library)
    monkeypatch.setattr(startup.paths, 'config_path', lambda: tmp_path / 'config.toml')
    monkeypatch.setattr(startup.logsetup, 'configure', lambda: None)
    monkeypatch.setattr('yt_playlist.core.updatecheck._raw_version', lambda: '0.0.0.dev0')

    with closing(Store(managed_library).conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:216])
        changed = conn.execute("SELECT * FROM prepared_outputs WHERE filename GLOB '*/by-id/*/*.json' "
                               "ORDER BY id LIMIT 1").fetchone()
        assert changed is not None
        raw = recommendation_outputs._read(changed['filename'], changed['content_digest'], changed['byte_size'])
        new_path, _, _ = recommendation_outputs.publish(recommendation_outputs.directory(conn), raw)
        assert new_path != changed['filename']
        with conn.atomic():
            recommendation_outputs._relocate(conn, {('prepared_outputs', changed['id']): new_path})

    def interrupt_after_one_page(conn, after):
        if batches:
            raise InterruptedError('recommendation preparation interrupted')
        worked = copy(conn, after)
        batches.append(worked)
        return worked

    with monkeypatch.context() as interrupted:
        interrupted.setattr(chunk_migration, '_copy_page', interrupt_after_one_page)
        with pytest.raises(InterruptedError, match='preparation interrupted'):
            startup.build_app()

    changes = regular.CHANGES
    chunk_change = changes[216]
    cutover_times = []
    cutover_counts = []

    def timed_install(conn, context):
        source_count = conn.execute(count_sql).fetchone()[0]
        start = perf_counter()
        chunk_change.apply(conn, context)
        cutover_times.append(perf_counter() - start)
        cutover_counts.append((source_count, conn.execute(count_sql).fetchone()[0]))

    selected_sql = ('SELECT generation_id,surface FROM recommendation_generation_surfaces '
                    'WHERE selected=1 ORDER BY 1,2')
    with closing(Store(managed_library).conn) as conn:
        assert conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == 216
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='recommendation_chunks'").fetchone()
        staged = conn.execute('SELECT count(*) FROM collection_file_writes WHERE chunk_id IS NOT NULL').fetchone()[0]
        assert staged == chunk_migration.PAGE_SIZE
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name GLOB 'prepare_recommendation_chunk_*'").fetchone()
        rows = conn.execute('SELECT * FROM prepared_outputs ORDER BY id').fetchall()
        assert rows
        old = list(map(tuple, rows))
        references = list(recommendation_outputs.references(conn))
        boundaries = sorted({
            (row['generation_id'], row['surface'], row['pool_id'], bound + offset)
            for row in rows if row['pool_id'] is not None
            for bound in (row['first_rank'], row['last_rank'])
            for offset in (-1, 0, 1)
        })
        assert boundaries
        expected = [rank_owner(conn, rank_query('prepared_outputs'), key) for key in boundaries]
        selected = list(map(tuple, conn.execute(selected_sql)))
        assert selected

    start = perf_counter()
    with monkeypatch.context() as measured:
        measured.setattr(regular, 'CHANGES', (*changes[:216], replace(chunk_change, apply=timed_install), *changes[217:]))
        app = startup.build_app()
    built = perf_counter()
    [(source_count, destination_count)] = cutover_counts
    assert destination_count == source_count - 1
    with closing(app.state.ctx.store.conn) as conn, TestClient(app, base_url='http://localhost') as client:
        status = client.get('/bridge/status')
        first_response = perf_counter()
        assert status.status_code == 200 and 'now_playing' in status.json()
        assert client.get('/').status_code == 200
        assert list(map(tuple, conn.execute(selected_sql))) == selected
        assert conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == len(changes)
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='prepared_outputs'").fetchone()
        assert not conn.execute('SELECT 1 FROM settings WHERE key=?', (chunk_migration.CURSOR,)).fetchone()
        assert list(map(tuple, conn.execute(f'SELECT * FROM {recommendation_chunks.VIEW} ORDER BY id'))) == old
        assert list(recommendation_outputs.references(conn)) == references
        lookup = rank_query(recommendation_chunks.VIEW)
        assert [rank_owner(conn, lookup, key) for key in boundaries] == expected
        rank_plan = [row[3] for row in conn.execute('EXPLAIN QUERY PLAN ' + lookup, boundaries[0])]
        assert any('SEARCH' in step and 'uq_recommendation_chunk_receipt_rank' in step for step in rank_plan), rank_plan
        example = rows[0]
        point_plan = [row[3] for row in conn.execute(
            f'EXPLAIN QUERY PLAN SELECT id FROM {recommendation_chunks.VIEW} '
            'WHERE generation_id=? AND surface=? AND chunk_key=?',
            (example['generation_id'], example['surface'], example['chunk_key']))]
        assert any('SEARCH' in step and 'uq_recommendation_chunk_receipt_key' in step for step in point_plan), point_plan
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    report_file('recommendation-chunk-receipts-c217.json').write_text(json.dumps({
        'chunks': len(rows), 'file_references': len(references), 'rank_boundary_checks': len(boundaries),
        'rows_committed_before_interruption': staged, 'relocated_predecessor_chunk': changed['id'],
        'cutover_sql_seconds': cutover_times[0],
        'restart_to_app_seconds': built - start, 'app_to_first_response_seconds': first_response - built,
        'selected_generations': len(selected), 'installed_schema': len(changes),
        'rank_plan': rank_plan, 'point_plan': point_plan,
        'source_table_count': source_count, 'destination_table_count': destination_count,
    }, indent=2) + '\n', encoding='utf-8')
