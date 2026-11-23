"""Compare physical publication manifests with their C180 embedded form."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.library_image.fixtures import managed_database, report_file
from yt_playlist.core import home_outputs, listening_outputs, publication_files, recommendation_outputs, recommendation_reports
from yt_playlist.core.migrations import home_display_predecessor, prepared_display_settings, publication_manifests, regular
from yt_playlist.core.store import Store
from yt_playlist.rec import home_cache


def _tables(conn):
    return {name for name, in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _legacy(conn, table, fields, order):
    records = {}
    for publication_id, *values in conn.execute(
            f'SELECT publication_id,{fields} FROM {table} ORDER BY publication_id,{order}'):
        records.setdefault(publication_id, []).append(values)
    return records


def _embedded(conn, table, field):
    return {identity: json.loads(raw) for identity, raw in conn.execute(
        f'SELECT id,{field} FROM {table} WHERE json_array_length({field})>0')}


def test_publication_manifest_upgrade():
    report = {}
    path = managed_database(report=report)
    if path is None:
        pytest.skip('Requires a private managed C175 snapshot')
    coordinates = 'id,surface,chunk_key,filename,content_digest,byte_size'
    with closing(sqlite3.connect(path)) as conn:
        before = _tables(conn)
        if 'home_display_files' not in before:
            pytest.skip('Requires a private managed C175 snapshot')
        home = _legacy(conn, 'home_display_files', coordinates, 'CAST(chunk_key AS INTEGER)')
        listening = _legacy(conn, 'listening_output_files',
            coordinates + ',built_at,position,from_day,until_day,chunk_bytes', 'position IS NULL,position,surface,chunk_key')
        groups = _legacy(conn, 'home_display_group_roots', 'field,kind,external_key', 'field')
        ranks = _legacy(conn, 'home_display_recommendation_roots', 'field,kind,external_key,generation_id',
                        'field,generation_id')
        policies = _legacy(conn, 'home_display_policies', 'raw_key,policy_version,resolution_reason', 'raw_key')
        assert all((home, listening, groups, ranks, policies))
        clicked = list(map(tuple, conn.execute('SELECT generation_id,lane,epoch,built_at,include_genres,avoid_genres '
                                              'FROM clicked_library_pools ORDER BY generation_id')))
        home_bodies = {surface: home_display_predecessor.encoded(conn, surface)
                       for surface, in conn.execute('SELECT surface FROM home_display_publications WHERE is_current=1')}
        listening_bodies = {}
        for surface, key, filename, digest, size in conn.execute('''
            SELECT f.surface,f.chunk_key,f.filename,f.content_digest,f.byte_size FROM listening_output_files f
            JOIN listening_output_publications p ON p.id=f.publication_id
            WHERE p.slot IS NOT NULL AND (f.chunk_key<>'' OR p.slot='current') ORDER BY p.slot DESC'''):
            listening_bodies[surface + ':' + key if key else surface] = recommendation_outputs.read(
                filename, digest, size, surface=surface, chunk_key=key)
        assert home_bodies['home_prepared'] and listening_bodies['listening_analytics']
        assert listening_bodies['listening_edition']

    store = Store(path)
    with closing(store.conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:180])
        after = _tables(conn)
        assert set(publication_manifests.RETIRED) <= before - after and not after - before
        for field, expected in (('files', home), ('group_roots', groups),
                                ('recommendation_roots', ranks), ('policy_roots', policies)):
            assert _embedded(conn, 'home_display_publications', field) == expected
        assert _embedded(conn, 'listening_output_publications', 'files') == listening
        assert list(map(tuple, conn.execute('SELECT id,clicked_lane,clicked_epoch,clicked_built_at,clicked_include_genres,'
                     'clicked_avoid_genres FROM recommendation_generations WHERE clicked_lane IS NOT NULL ORDER BY id'))) == clicked

        regular.migrate(conn, changes=regular.CHANGES[:198])
        prior_tables = _tables(conn)
        file_references = list(recommendation_outputs.references(conn))
        report_count = conn.execute('SELECT count(*) FROM recommendation_report_publications WHERE selected=1').fetchone()[0]
        assert report_count
        report_facts = list(conn.execute('SELECT surface,content_digest,total_bytes,part_count FROM '
                                         'recommendation_report_publications WHERE selected=1 ORDER BY surface'))
        all_headers = sum(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                          for table in prepared_display_settings.RETIRED)
        store.init_schema()
        assert set(prepared_display_settings.RETIRED) <= prior_tables - _tables(conn)
        assert not conn.execute("SELECT 1 FROM settings WHERE key GLOB 'prepared_home:*' "
                                "OR key GLOB 'prepared_listening:*' OR key GLOB 'prepared_report:*'").fetchone()
        owners = conn.execute('SELECT count(*) FROM collection_file_writes WHERE display_key IS NOT NULL').fetchone()[0]
        assert owners == all_headers
        plan = conn.execute(f'EXPLAIN QUERY PLAN SELECT value FROM {publication_files.MANIFESTS} WHERE key=?',
                            (home_outputs.PREFIX + 'home_prepared',)).fetchall()
        assert any(publication_files.INDEX in row[-1] for row in plan)
        for surface, digest, size, parts in report_facts:
            manifest = recommendation_reports.manifest(conn, surface)
            assert (manifest['content_digest'], manifest['total_bytes'], manifest['part_count']) == (digest, size, parts)
        assert list(recommendation_outputs.references(conn)) == file_references
        assert {surface: home_outputs.encoded(conn, surface) for surface in home_bodies} == home_bodies
        assert {surface: store.listening_analytics.encoded(surface) for surface in listening_bodies} == listening_bodies
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        edition = json.loads(listening_bodies['listening_edition'])
        refreshed = {**edition, 'build_check': 'refreshed'}
        with store.read_snapshot() as snapshot:
            prior = snapshot.store
            assert prior.listening_analytics.prepared('listening_edition') == edition
            home_cache.put(store, 'home_choice', {'build_check': 'refreshed'}, 10)
            store.listening_analytics.publish(json.dumps(refreshed), 10, 'listening_edition')
            collector = recommendation_outputs.OrphanCollector(recommendation_outputs.directory(conn))
            try:
                assert collector.step(conn).blocked
            finally:
                collector.close()
            assert prior.listening_analytics.prepared('listening_edition') == edition
            assert home_outputs.encoded(prior.conn, 'home_choice') == home_bodies.get('home_choice')
            assert home_outputs.encoded(prior.conn, 'home_prepared') == home_bodies['home_prepared']
        assert store.listening_analytics.prepared('listening_edition') == refreshed
        assert home_cache.read(store, 'home_choice') == {'build_check': 'refreshed'}
        assert store.listening_analytics.encoded() == listening_bodies['listening_analytics']

    store = Store(path)
    with closing(store.conn) as conn:
        store.init_schema()
        assert store.listening_analytics.prepared('listening_edition') == refreshed
        assert home_cache.read(store, 'home_choice') == {'build_check': 'refreshed'}
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    report.update(tables_before=len(before), tables_after=len(after), removed=sorted(before - after),
                  home_files=sum(map(len, home.values())), listening_files=sum(map(len, listening.values())),
                  group_roots=sum(map(len, groups.values())), recommendation_roots=sum(map(len, ranks.values())),
                  policy_roots=sum(map(len, policies.values())), populated_clicked_generations=len(clicked),
                  prior_reader_kept_publication=True, reopened=True, foreign_key_errors=0)
    report.update(selected_reports=report_count, migrated_display_headers=all_headers, file_owned_manifests=owners,
                  unchanged_file_references=len(file_references))
    report_file('publication-manifests-c211.json').write_text(json.dumps(report, indent=2) + '\n')


def test_display_file_owners_on_populated_upgrade(managed_library):
    store = Store(managed_library)
    with closing(store.conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:210])
        predicate = "key GLOB 'prepared_home:*' OR key GLOB 'prepared_listening:*' OR key GLOB 'prepared_report:*'"
        expected = {key: json.loads(value) for key, value in conn.execute(f'SELECT key,value FROM settings WHERE {predicate}')}
        before = _tables(conn)
        references = list(recommendation_outputs.references(conn))
        assert references and expected
        bodies = {surface: home_outputs.read_manifest(surface, row['content_digest'], row['total_bytes'],
                  [tuple(part[2:6]) for part in row['files']])
                  for surface in home_outputs.SURFACES if (row := expected.get(home_outputs.PREFIX + surface))}
        store.init_schema()
        assert _tables(conn) == before
        assert dict((key, json.loads(value)) for key, value in conn.execute(
            f'SELECT key,value FROM {publication_files.MANIFESTS}')) == expected
        assert not conn.execute(f'SELECT 1 FROM settings WHERE {predicate}').fetchone()
        assert list(recommendation_outputs.references(conn)) == references
        assert {surface: home_outputs.encoded(conn, surface) for surface in bodies} == bodies
        assert any(bodies.values())
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
        owners = dict(conn.execute('SELECT kind,count(*) FROM collection_file_writes WHERE display_key IS NOT NULL GROUP BY kind'))
        plan = [line[-1] for line in conn.execute(f'EXPLAIN QUERY PLAN SELECT value FROM {publication_files.MANIFESTS} '
                                                 'WHERE key=?', ('prepared_home:home_prepared',))]
        assert any(publication_files.INDEX in line for line in plan)
    report_file('display-file-owners-c211.json').write_text(json.dumps({
        'table_count': len(before), 'manifests': len(expected), 'owned_receipts': owners,
        'unchanged_file_references': len(references), 'home_outputs_verified': sorted(bodies), 'key_plan': plan,
    }, indent=2) + '\n', encoding='utf-8')
