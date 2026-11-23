"""Reopen selected evidence and recording-association history after migration."""
from contextlib import closing
from dataclasses import replace
import json
import math
import sqlite3
from time import perf_counter

import pytest
from fastapi.testclient import TestClient

from tests.library_image.fixtures import report_file
from yt_playlist import __main__ as startup
from yt_playlist.core import association_work, legacy_journals, metadata, metadata_choice_selections
from yt_playlist.core import metadata_runtime_files, metadata_selections
from yt_playlist.core import runtime_metadata_choices
from yt_playlist.core.migrations import metadata_history_owners, metadata_selection_owners, regular
from yt_playlist.core.store import Store


def _interrupt_after_page(start, migration, monkeypatch):
    copy = migration._copy_page
    committed = []

    def copy_once(*args):
        if committed:
            raise InterruptedError('metadata preparation interrupted')
        worked = copy(*args)
        if worked:
            committed.append(worked)
        return worked

    with monkeypatch.context() as interrupted:
        interrupted.setattr(migration, '_copy_page', copy_once)
        with pytest.raises(InterruptedError, match='preparation interrupted'):
            start()


def _measure_cutover(conn, version):
    changes = regular.CHANGES[:version]
    times = []

    def install(conn, context):
        start = perf_counter()
        changes[-1].apply(conn, context)
        times.append(perf_counter() - start)

    regular.migrate(conn, changes=(*changes[:-1], replace(changes[-1], apply=install)))
    return times[0]


def test_normal_startup_finishes_pending_metadata_upgrade(managed_library, monkeypatch):
    monkeypatch.setattr(startup.paths, 'db_path', lambda: managed_library)
    monkeypatch.setattr(startup.logsetup, 'configure', lambda: None)
    monkeypatch.setattr('yt_playlist.core.updatecheck._raw_version', lambda: '0.0.0.dev0')

    _interrupt_after_page(startup.build_app, metadata_selection_owners, monkeypatch)
    with closing(Store(managed_library).conn) as conn:
        assert conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == 213
        selection_count = conn.execute('SELECT count(*) FROM recording_metadata_selections').fetchone()[0]

    _interrupt_after_page(startup.build_app, metadata_history_owners, monkeypatch)
    with closing(Store(managed_library).conn) as conn:
        assert conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == 215
        history_count = conn.execute('SELECT count(*) FROM catalog_metadata_audit WHERE recording_id<>0').fetchone()[0]

    started = perf_counter()
    app = startup.build_app()
    built = perf_counter()
    with closing(app.state.ctx.store.conn) as conn, TestClient(app, base_url='http://localhost') as client:
        assert conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == len(regular.CHANGES)
        response = client.get('/bridge/status')
        first_response = perf_counter()
        assert response.status_code == 200
        assert 'now_playing' in response.json()
        assert client.get('/').status_code == 200
        assert conn.execute('SELECT count(*) FROM current_metadata_selections').fetchone()[0] == selection_count
        assert conn.execute('SELECT count(*) FROM recording_metadata_history').fetchone()[0] == history_count

    report_file('metadata-startup.json').write_text(json.dumps({
        'installed_schema': len(regular.CHANGES), 'selections': selection_count, 'history_events': history_count,
        'restart_to_app_seconds': built - started, 'app_to_first_response_seconds': first_response - built,
    }, indent=2) + '\n', encoding='utf-8')


def test_source_owned_metadata_and_corrections_survive_an_upgrade(managed_library):
    store = Store(managed_library)
    with closing(store.conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:204])
        with conn.atomic():
            source = conn.execute('SELECT id,recording_id,video_id FROM track_sources '
                                  'WHERE video_id IS NOT NULL ORDER BY id LIMIT 1').fetchone()
            selected = conn.execute('INSERT INTO metadata_observations(entry_id,recording_id,input_video_id,field_scope,field,'
                "value_kind,text_value,observed_at,origin,legacy_source) VALUES (?,?,?,'release','label','value',?,1,'user','user')",
                (*source, 'Example selected label')).lastrowid
            conn.execute("INSERT INTO metadata_choice_heads(entry_id,field,observation_id) VALUES (?,'label',?) "
                         'ON CONFLICT(entry_id,field) DO UPDATE SET observation_id=excluded.observation_id',
                         (source[0], selected))
            association, video = conn.execute('SELECT id,video_id FROM item_recording_associations '
                                              'WHERE work_plan_json IS NULL ORDER BY id LIMIT 1').fetchone()
            conn.execute("UPDATE item_recording_associations SET work_plan_json='{}',work_assigned=1,work_keys=1 WHERE id=?",
                         (association,))
        choices = list(map(tuple, conn.execute('SELECT entry_id,field,observation_id '
                                               'FROM metadata_choice_heads ORDER BY entry_id,field')))
        fields = list(map(tuple, conn.execute('SELECT * FROM catalog_tracks ORDER BY id')))
        history = list(map(tuple, conn.execute('SELECT id,video_id,recording_id,previous_recording_id,source,evidence,at '
                                               'FROM item_recording_associations ORDER BY id')))
        pending = list(map(tuple, conn.execute('SELECT id,video_id,work_plan_json,'
            + ','.join('work_' + step for step in association_work.STEPS)
            + ' FROM item_recording_associations WHERE work_plan_json IS NOT NULL ORDER BY id')))
        store.init_schema()

    store = Store(managed_library)
    with closing(store.conn) as conn:
        store.init_schema()
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name IN "
                                "('metadata_choice_heads','item_recording_associations')").fetchone()
        assert list(map(tuple, conn.execute('SELECT s.id,h.key,h.atom FROM track_sources s,json_each(s.choice_observations) h '
                                            'ORDER BY s.id,h.key'))) == choices
        assert list(map(tuple, conn.execute('SELECT * FROM catalog_tracks ORDER BY id'))) == fields
        assert metadata_choice_selections.get(conn, source[0], 'label')['id'] == selected
        assert list(map(tuple, conn.execute('SELECT h.value->>0,s.video_id,h.value->>1,h.value->>2,h.value->>3,h.value->>4,h.value->>5 '
                                            'FROM track_sources s,json_each(s.association_history) h ORDER BY 1'))) == history
        assert list(map(tuple, conn.execute("SELECT association_work->>'id',video_id,association_work->>'plan_json',"
            + ','.join(f"association_work->>'{step}'" for step in association_work.STEPS)
            + ' FROM track_sources WHERE association_work IS NOT NULL ORDER BY 1'))) == pending
        lookup = [result[3] for result in conn.execute('EXPLAIN QUERY PLAN SELECT id FROM track_sources WHERE '
                                                     + association_work.PENDING, (association,))]
        assert any('SEARCH' in line and association_work.MARKER in line for line in lookup), lookup
        with conn.atomic():
            association_work.complete(conn, association, 'discovery')
            state = association_work.pending(conn, video)
            assert (state['assigned'], state['discovery'], state['keys'], state['redirected'], state['model']) == (1, 1, 1, 0, 0)
            association_work.finish(conn, association)
            assert association_work.pending(conn, video) is None
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    report_file('track-source-choices-c206.json').write_text(json.dumps({
        'choice_observations': len(choices), 'catalog_tracks': len(fields), 'history_entries': len(history),
        'pending_corrections': len(pending), 'lookup': lookup,
    }, indent=2) + '\n', encoding='utf-8')


def test_current_metadata_selection_owners_survive_an_upgrade(managed_library, monkeypatch):
    store = Store(managed_library)
    order = ' ORDER BY recording_id,entry_id,field'
    with closing(store.conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:213])
        with conn.atomic():
            entry, recording, video = conn.execute('SELECT id,recording_id,video_id FROM track_sources '
                                                   'WHERE video_id IS NOT NULL ORDER BY id LIMIT 1').fetchone()
            at = math.nextafter(17.0, math.inf)
            entry_at = math.nextafter(at, math.inf)
            observation = conn.execute('INSERT INTO metadata_observations(entry_id,recording_id,input_video_id,field_scope,field,'
                "value_kind,text_value,observed_at,origin,legacy_source) VALUES (?,?,?,'release','label','value',?,?,'selection_snapshot','fixture')",
                (entry, recording, video, 'Historical selected label', at)).lastrowid
            conn.execute('INSERT INTO recording_metadata_selections(entry_id,field,observation_id,attribution_status,selected_at) '
                         "VALUES (?,'label',?,'evidenced',?) ON CONFLICT(entry_id,field) DO UPDATE SET "
                         'observation_id=excluded.observation_id,attribution_status=excluded.attribution_status,selected_at=excluded.selected_at',
                         (entry, observation, entry_at))
            conn.execute('INSERT INTO recording_metadata_selections(recording_id,field,attribution_status,legacy_source,selected_at) '
                         "VALUES (?,'genre','legacy_unresolved','historical provider',?) ON CONFLICT(recording_id,field) DO UPDATE SET "
                         'attribution_status=excluded.attribution_status,legacy_source=excluded.legacy_source,selected_at=excluded.selected_at',
                         (recording, at))
        _interrupt_after_page(lambda: regular.migrate(conn, changes=regular.CHANGES[:214]),
                              metadata_selection_owners, monkeypatch)

    with closing(Store(managed_library).conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:213])
        staged = conn.execute("SELECT count(*) FROM track_sources WHERE metadata_selections<>'{}'").fetchone()[0]
        assert staged == metadata_selection_owners.PAGE_SIZE
        assert conn.execute('SELECT 1 FROM item_field_sources LIMIT 1').fetchone()
        assert conn.execute("SELECT metadata_selections->>'$.label[3]' FROM track_sources WHERE id=?",
                            (entry,)).fetchone()[0] == entry_at
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name GLOB 'prepare_metadata_selection_*'").fetchone()
        before = list(map(tuple, conn.execute('SELECT * FROM recording_metadata_selections' + order)))
        table_count = conn.execute("SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'").fetchone()[0]
        cutover = _measure_cutover(conn, 214)
        destination_count = conn.execute("SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'").fetchone()[0]
        assert destination_count == table_count - 1

    store = Store(managed_library)
    with closing(store.conn) as conn:
        store.init_schema()
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='recording_metadata_selections'").fetchone()
        assert not conn.execute("SELECT 1 FROM settings WHERE key GLOB 'prepare_metadata_selections_*'").fetchone()
        assert list(map(tuple, conn.execute('SELECT * FROM current_metadata_selections' + order))) == before
        with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'), conn.atomic():
            conn.execute('DELETE FROM metadata_observations WHERE id=?', (observation,))
        with conn.atomic():
            provider = conn.execute('SELECT metadata_provider_id FROM enrichment_provider_config '
                                    'WHERE metadata_provider_id IS NOT NULL ORDER BY metadata_provider_id LIMIT 1').fetchone()[0]
            metadata_selections.save_runtime(conn, 'entry', entry, 'label', provider)
            metadata_selections.save_runtime(conn, 'recording', recording, 'genre', provider)
        assert tuple(conn.execute('SELECT observation_id,selected_at,runtime_provider_id FROM current_metadata_selections '
                                  "WHERE entry_id=? AND field='label'", (entry,)).fetchone()) == (observation, entry_at, provider)
        assert tuple(conn.execute('SELECT attribution_status,legacy_source,selected_at,runtime_provider_id FROM current_metadata_selections '
                                  "WHERE recording_id=? AND field='genre'", (recording,)).fetchone()) == (
                                      'legacy_unresolved', 'historical provider', at, provider)
        plans = {}
        for view, key, identity in (('item_field_sources', 'entry_id', entry),
                                    ('recording_field_sources', 'recording_id', recording)):
            plans[view] = [row[3] for row in conn.execute(f'EXPLAIN QUERY PLAN SELECT source FROM {view} '
                                                        f'WHERE {key}=? AND field=?', (identity, 'label'))]
            assert any('SEARCH subject USING INTEGER PRIMARY KEY' in line for line in plans[view]), plans[view]
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    report_file('metadata-selection-owners-c214.json').write_text(json.dumps({
        'recording_selections': sum(row[0] is not None for row in before),
        'upload_selections': sum(row[1] is not None for row in before),
        'uploads_committed_before_interruption': staged, 'cutover_sql_seconds': cutover,
        'source_table_count': table_count, 'destination_table_count': destination_count,
        'observation_and_unresolved_fixtures_preserved': True, 'lookup_plans': plans,
    }, indent=2) + '\n', encoding='utf-8')


def test_runtime_choices_keep_values_identities_and_files(managed_library):
    store = Store(managed_library)
    with closing(store.conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:214])
        original = list(map(tuple, conn.execute('SELECT * FROM metadata_runtime_choices ORDER BY id')))
        assert original
        previous_sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='metadata_runtime_choices'").fetchone()[0]
        before = [item for item in metadata_runtime_files.active_references(conn, choice_relation='metadata_runtime_choices')
                  if item[0] == 'choices']
        catalog = list(map(tuple, conn.execute('SELECT * FROM catalog_tracks ORDER BY id')))
        count_sql = "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'"
        source_count = conn.execute(count_sql).fetchone()[0]
        regular.migrate(conn, changes=regular.CHANGES[:215])
        destination_count = conn.execute(count_sql).fetchone()[0]
        assert destination_count == source_count - 1

    with closing(Store(managed_library).conn) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='metadata_runtime_choices'").fetchone()
        assert list(map(tuple, conn.execute('SELECT * FROM current_runtime_choices ORDER BY id'))) == original
        assert list(map(tuple, conn.execute('SELECT * FROM catalog_tracks ORDER BY id'))) == catalog
        after = [item for item in metadata_runtime_files.active_references(conn) if item[0] == 'choices']
        assert sorted(after) == sorted(before)
        for identity, entry, field, provider, revision, *_ in original:
            assert tuple(runtime_metadata_choices.head(conn, entry, field)) == (identity, revision)
        retained = conn.execute("SELECT max(owner_id->>3) FROM collection_file_writes WHERE kind='runtime-metadata' "
                                "AND owner_id->>0='choices'").fetchone()[0]
        with conn.atomic():
            following = runtime_metadata_choices.next_identity(conn)
            assert following > max(previous_sequence, retained or 0)
            assert runtime_metadata_choices.next_identity(conn) == following + 1
            entry = conn.execute('SELECT id FROM track_sources WHERE runtime_metadata_choices=? LIMIT 1', ('{}',)).fetchone()[0]
            value = math.nextafter(70.0, math.inf)
            runtime_metadata_choices.save(conn, entry, 2, following, provider, 1, 4, None, value, None)
            assert tuple(conn.execute('SELECT id,revision,number_value,typeof(number_value) '
                                      'FROM current_runtime_choices WHERE entry_id=? AND field=2', (entry,)).fetchone()) == (
                                          following, 1, value, 'real')
            runtime_metadata_choices.save(conn, entry, 2, following, provider, 2, 0, None, None, None)
            assert tuple(conn.execute('SELECT revision,kind,number_value FROM current_runtime_choices '
                                      'WHERE entry_id=? AND field=2', (entry,)).fetchone()) == (2, 0, None)
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    report_file('runtime-choice-owners-c215.json').write_text(json.dumps({
        'choices': len(original), 'uploads': len({row[1] for row in original}), 'revision_references': len(before),
        'source_table_count': source_count, 'destination_table_count': destination_count,
        'exact_catalog_and_choices_preserved': True,
    }, indent=2) + '\n', encoding='utf-8')


def test_recordings_retain_selection_and_correction_history(managed_library, monkeypatch):
    store = Store(managed_library)
    with closing(store.conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:215])
        insert = ('INSERT INTO catalog_metadata_audit(entry_id,recording_id,field,source,operation,value,changed_at) '
                  'VALUES (?,?,?,?,?,?,?)')
        entry, recording = conn.execute('SELECT id,recording_id FROM track_sources WHERE runtime_metadata_choices=? '
                                        'ORDER BY id LIMIT 1', ('{}',)).fetchone()
        base_events = conn.execute('SELECT count(*) FROM catalog_metadata_audit').fetchone()[0]
        correction_time = math.nextafter(17.0, math.inf)
        with conn.atomic():
            previous = conn.execute('SELECT min(id)-1 FROM catalog_metadata_audit').fetchone()[0]
            conn.execute('INSERT INTO catalog_metadata_audit(id,entry_id,recording_id,field,source,operation,value,changed_at) '
                         "VALUES (?,?,?,'label','historical','select','Recovered original position',1)",
                         (previous, entry, recording))
            conn.execute(insert, (entry, recording, 'title', 'user', 'edit', 'Exact corrected title', correction_time))
            conn.execute(insert, (entry, recording, 'unknown_historical_field', 'user', 'resolve', b'\xc0\x01', 4.0))
            missing = conn.execute(insert, (0, 0, 'label', 'historical', 'select', b'\x00\x13', 1.0)).lastrowid
        before = list(map(tuple, conn.execute('SELECT *,typeof(value),typeof(changed_at) FROM catalog_metadata_audit '
                                             'WHERE recording_id<>0 ORDER BY id')))
        assert conn.execute('SELECT title FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0] == 'Exact corrected title'
        catalog = list(map(tuple, conn.execute('SELECT * FROM catalog_tracks ORDER BY id')))
        _interrupt_after_page(lambda: regular.migrate(conn, changes=regular.CHANGES[:216]),
                              metadata_history_owners, monkeypatch)

    with closing(Store(managed_library).conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:215])
        staged = conn.execute('SELECT sum(json_array_length(metadata_provider_history)+json_array_length(metadata_corrections)) '
                              'FROM recordings').fetchone()[0]
        assert staged == metadata_history_owners.PAGE_SIZE
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name GLOB 'prepare_metadata_history_*'").fetchone()
        count_sql = "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'"
        source_count = conn.execute(count_sql).fetchone()[0]
        cutover = _measure_cutover(conn, 216)
        destination_count = conn.execute(count_sql).fetchone()[0]
        assert destination_count == source_count - 1

    store = Store(managed_library)
    with closing(store.conn) as conn:
        store.init_schema()
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='catalog_metadata_audit'").fetchone()
        assert not conn.execute('SELECT 1 FROM settings WHERE key=?', (metadata_history_owners.CURSOR,)).fetchone()
        after = list(map(tuple, conn.execute('SELECT *,typeof(value),typeof(changed_at) '
                                            'FROM recording_metadata_history ORDER BY id')))
        assert after == before
        assert list(map(tuple, conn.execute('SELECT * FROM catalog_tracks ORDER BY id'))) == catalog
        archived = legacy_journals.read(conn, 'metadata', 'recording_metadata_history', str(missing))
        assert json.loads(archived['payload']) == {'recording_id': 0,
            'event': [missing, 0, 'label', 'historical', 'select', {'hex': '0013'}, None, 1.0]}
        assert metadata.has_choice(conn, entry, 'title')
        store.metadata_choices.choose(entry, 'title', withdraw=True, now=20.0)
        assert not metadata.has_choice(conn, entry, 'title')
        reset = conn.execute('SELECT id,operation,changed_at FROM recording_metadata_history '
                             "WHERE recording_id=? AND field='title' ORDER BY id DESC LIMIT 1", (recording,)).fetchone()
        assert tuple(reset) == (missing + 1, 'reset', 20.0)
        plan = [row[3] for row in conn.execute('EXPLAIN QUERY PLAN SELECT value FROM recording_metadata_history '
                                              'WHERE recording_id=? AND field=?', (recording, 'title'))]
        assert sum('SEARCH r USING INTEGER PRIMARY KEY' in row for row in plan) == 2, plan
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    report_file('recording-metadata-history-c216.json').write_text(json.dumps({
        'installed_events': base_events, 'retained_recording_events': len(before), 'archived_orphan_fixture': True,
        'events_committed_before_interruption': staged, 'cutover_sql_seconds': cutover,
        'source_table_count': source_count, 'destination_table_count': destination_count, 'lookup_plan': plan,
    }, indent=2) + '\n', encoding='utf-8')
