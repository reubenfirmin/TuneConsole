"""Verify the feedback settings cutover on a private library snapshot."""
from contextlib import closing
import json
import math
import sqlite3

import pytest

from tests.library_image.fixtures import report_file
from yt_playlist.core import collection_recovery, discovery_provenance, experiment_decision_state, playback_bindings, rating_actions
from yt_playlist.core import recommendation_impressions as impressions, taste_axes
from yt_playlist.core.migrations import regular
from yt_playlist.core.migrations.playback_binding_storage import KEY as PREVIOUS_BINDINGS
from yt_playlist.core.store import Store


def test_feedback_settings_preserve_recent_choices_and_saved_experiment_evidence(managed_library):
    store = Store(managed_library)
    with closing(store.conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:206])
        bindings = [dict(row) for row in conn.execute(
            'SELECT * FROM listening_playback_bindings ORDER BY bound_at DESC,account_id,playlist_scope_id,video_id')]
        discovery_views = {}
        for kind, subject in discovery_provenance.SUBJECTS.items():
            discovery_views[kind] = list(map(tuple, conn.execute(f'SELECT {subject},view_id,shown_at FROM discovery_impressions '
                                                               f'WHERE {subject} IS NOT NULL ORDER BY 1,2')))
        album_offers = list(map(tuple, conn.execute('SELECT * FROM discovered_albums ORDER BY browse_id')))
        identity = conn.execute('SELECT COALESCE(max(id),0)+1 FROM experiment_decisions').fetchone()[0]
        at = 1795369121.9246054
        evidence = {'ppr': None, 'cosine': {'completion': 4, 'clusters': 2}}
        decision = dict(decision='retire', cohort='actual-library', reason='Verify existing decision evidence',
                        at=at, powered=False, basis='operator retirement', live_evidence=evidence)
        with conn.atomic():
            ref = collection_recovery.publish(conn, 'experiment-decision', identity, evidence)
            conn.execute('INSERT INTO experiment_decisions VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (identity, decision['decision'], decision['cohort'], decision['reason'], at, 0,
                          decision['basis'], *ref))
        assert any(owner == 'experiment_decisions' and owner_id == identity for owner, owner_id, *_ in
                   collection_recovery._backup_repository(conn).references())
        store.init_schema()

    store = Store(managed_library)
    with closing(store.conn) as conn:
        store.init_schema()
        assert not conn.execute('SELECT value FROM settings WHERE key IN (?,?)',
                                (PREVIOUS_BINDINGS, playback_bindings.KEY)).fetchone()
        located = 0
        for binding in bindings:
            account, scope, video, opportunity, option, bound = binding.values()
            playlist = conn.execute('SELECT provider_playlist_id FROM playlist_scopes WHERE id=?', (scope,)).fetchone()[0]
            receipt = playback_bindings.lookup(conn, account, playlist, video, before=bound, through=bound)
            assert receipt and receipt['response_id']
            observed = store.rec_ledger.choice_playback(account, playlist, video, bound, 0)
            expected = dict(account_id=account, playlist=playlist, video=video,
                            opportunity_id=opportunity, option_id=option, at=bound)
            assert {key: observed[key] for key in expected} == expected
            located += 1
        assert experiment_decision_state.records(conn)[-1] == decision
        for kind, table in discovery_provenance.OFFER_OWNERS.items():
            after = list(map(tuple, conn.execute(f'SELECT t.id,v.key,v.atom FROM {table} t,json_each(t.discovery_views) v '
                                                "WHERE t.discovery_views<>'{}' ORDER BY 1,2")))
            assert after == discovery_views[kind]
        assert list(map(tuple, conn.execute('SELECT * FROM discovered_albums ORDER BY browse_id'))) == album_offers
        assert not any(owner == 'experiment_decisions' for owner, *_ in collection_recovery.references(conn))
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name IN "
                                "('experiment_decisions','listening_playback_bindings','discovery_impressions')").fetchone()
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    report_file('feedback-settings-c209.json').write_text(json.dumps({
        'playback_bindings': len(bindings), 'indexed_response_receipts': located,
        'experiment_evidence_preserved': True,
        'discovery_views': {kind: len(rows) for kind, rows in discovery_views.items()},
    }, indent=2) + '\n', encoding='utf-8')


def test_impression_counts_survive_upgrade_and_continue_rotation(managed_library):
    count_sql = "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'"
    with closing(Store(managed_library).conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:220])
        with conn.atomic():
            conn.executemany('INSERT INTO rec_impressions VALUES (?,?,?,?)', (
                ('foreign:surface', 'Historical \\"quoted\\" key', 3, None),
                ('listening_editorial', 'fixture', 2, math.nextafter(20.0, math.inf))))
        rows = list(map(tuple, conn.execute('SELECT * FROM rec_impressions ORDER BY surface,item_key')))
        surfaces = {surface for surface, *_ in rows}
        source_count = conn.execute(count_sql).fetchone()[0]
        regular.migrate(conn, changes=regular.CHANGES[:221])
        destination_count = conn.execute(count_sql).fetchone()[0]
        assert destination_count == source_count - 1

    store = Store(managed_library)
    store.init_schema()
    with closing(store.conn) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='rec_impressions'").fetchone() is None
        actual = sorted((surface, item, count, at) for surface in surfaces
                        for item, (count, at) in impressions.counts(conn, surface).items())
        assert actual == rows
        before = store.rec_ledger.card_views('comfort')
        assert store.rec_ledger.bump_card_view('comfort', 1795372800) == before + 1
        assert store.rec_ledger.card_views('comfort') == before + 1
        assert store.rec_ledger.record_story_views(['fixture'], 100)
        assert not store.rec_ledger.record_story_views(['fixture'], 1000)
        assert store.rec_ledger.record_story_views(['fixture'], 100 + 6 * 3600)
        assert store.rec_ledger.story_views()['fixture'] == {'views': 2, 'last_shown': 21700}
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    report_file('recommendation-impression-counts-c221.json').write_text(json.dumps({
        'surface_counts': {surface: sum(row[0] == surface for row in rows) for surface in sorted(surfaces)},
        'counters': len(rows), 'source_table_count': source_count, 'destination_table_count': destination_count,
    }, indent=2) + '\n', encoding='utf-8')


def test_dated_rating_actions_reopen_on_player_events(managed_library):
    count_sql = "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'"
    with closing(Store(managed_library).conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:219])
        rows = list(map(tuple, conn.execute('SELECT * FROM listening_rating_actions ORDER BY id')))
        assert rows
        highest = rows[-1][0] + 7
        with conn.atomic():
            conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name='listening_rating_actions'", (highest,))
        source_count = conn.execute(count_sql).fetchone()[0]
        regular.migrate(conn, changes=regular.CHANGES[:220])
        destination_count = conn.execute(count_sql).fetchone()[0]
        assert destination_count == source_count - 1

    store = Store(managed_library)
    store.init_schema()
    with closing(store.conn) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='listening_rating_actions'").fetchone()
        assert list(map(tuple, conn.execute(f'SELECT * FROM {rating_actions.VIEW} ORDER BY id'))) == rows
        assert conn.execute(f'SELECT count(*) FROM player_events WHERE kind=?', (rating_actions.KIND,)).fetchone()[0] == len(rows)
        lookup = f'SELECT id,at FROM {rating_actions.VIEW} WHERE at>? ORDER BY at,id LIMIT 10'
        plan = [row[3] for row in conn.execute('EXPLAIN QUERY PLAN ' + lookup, (rows[0][-1],))]
        assert any('SEARCH' in step and 'ix_confirmed_rating_action_time' in step for step in plan), plan
        _, account, video, *_ = rows[-1]
        old = store.tracks.item_rating(account, video)
        status = 'INDIFFERENT' if old and old['status'] == 'LIKE' else 'LIKE'
        now = max(rows[-1][-1], old['observed_at'] if old else 0) + 10
        store.tracks.record_item_rating(account, video, status, source='api', now=now)
        assert tuple(conn.execute(f'SELECT id,account_id,video_id,status,at FROM {rating_actions.VIEW} '
                                  'ORDER BY id DESC LIMIT 1').fetchone()) == (highest + 1, account, video, status, now)
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    report_file('rating-action-events-c220.json').write_text(json.dumps({
        'rating_actions': len(rows), 'uploads': len({row[2] for row in rows}), 'range_plan': plan,
        'source_table_count': source_count, 'destination_table_count': destination_count,
    }, indent=2) + '\n', encoding='utf-8')


def test_graduation_history_reopens_with_each_taste_axis(managed_library):
    count_sql = "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'"
    with closing(Store(managed_library).conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:221])
        with conn.atomic():
            axis = taste_axes.identity(conn, 'audit:synthetic', create=True)
            now = math.nextafter(1795372800.0, math.inf)
            score = math.nextafter(1.0, math.inf)
            conn.execute('INSERT INTO recommendation_gradient_events(axis_id,source,score,factor,new_weight,created_at) '
                         'VALUES (?,?,?,?,?,?)', (axis, 'slider', score, 1.1, 1.2, now))
            high_water = conn.execute('SELECT max(id)+7 FROM recommendation_gradient_events').fetchone()[0]
            conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name='recommendation_gradient_events'", (high_water,))
        rows = list(map(tuple, conn.execute('SELECT * FROM recommendation_gradient_events ORDER BY id')))
        source_count = conn.execute(count_sql).fetchone()[0]
        regular.migrate(conn, changes=regular.CHANGES[:222])
        destination_count = conn.execute(count_sql).fetchone()[0]
        assert destination_count == source_count - 1

    store = Store(managed_library)
    store.init_schema()
    with closing(store.conn) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='recommendation_gradient_events'").fetchone()
        assert list(map(tuple, conn.execute(f'SELECT id,axis_id,source,score,factor,new_weight,created_at '
                                            f'FROM {taste_axes.GRADUATIONS} ORDER BY id'))) == rows
        assert len(store.rec_signals.recent_graduations(since=now, until=now)) == 1
        assert sum(store.rec_signals.graduation_log_counts().values()) == len(rows)
        store.rec_signals.log_graduation('audit:synthetic', 'slider', score, 1.1, 1.2, now)
        assert [row['id'] for row in store.rec_signals.recent_graduations(since=now, until=now)] == [high_water + 1, rows[-1][0]]
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
        installed_tables = conn.execute(count_sql).fetchone()[0]

    from yt_playlist.core.migrations import migrate
    with closing(Store(':memory:').conn) as fresh:
        migrate(fresh)
        assert fresh.execute(count_sql).fetchone()[0] == installed_tables

    report_file('taste-axis-graduations-c222.json').write_text(json.dumps({
        'events': len(rows), 'axes': len({row[1] for row in rows}),
        'source_table_count': source_count, 'destination_table_count': destination_count,
    }, indent=2) + '\n', encoding='utf-8')


def test_mode_offers_reopen_with_home_rotation(managed_library):
    from yt_playlist.core import mode_offers

    count_sql = "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'"
    with closing(Store(managed_library).conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:222])
        at = math.nextafter(1795372800.0, math.inf)
        epoch = conn.execute('SELECT max(epoch)+1 FROM rec_mode_impressions').fetchone()[0]
        mode = conn.execute('SELECT mode_id FROM taste_modes WHERE active=1 ORDER BY mode_id LIMIT 1').fetchone()[0]
        with conn.atomic():
            conn.executemany('INSERT INTO rec_mode_impressions VALUES (?,?,?,?,?,?)', (
                (epoch, 'wheelhouse', mode, 'ppr', at, None),
                (epoch, 'historical', None, 'cosine', at + 1, 9999999)))
        rows = list(map(tuple, conn.execute('SELECT * FROM rec_mode_impressions ORDER BY epoch,lane')))
        source_count = conn.execute(count_sql).fetchone()[0]
        regular.migrate(conn, changes=regular.CHANGES[:223])
        destination_count = conn.execute(count_sql).fetchone()[0]
        assert destination_count == source_count - 1

    store = Store(managed_library)
    store.init_schema()
    with closing(store.conn) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='rec_mode_impressions'").fetchone() is None
        assert list(map(tuple, conn.execute(f'SELECT * FROM {mode_offers.VIEW} ORDER BY epoch,lane'))) == rows
        assert store.modes.epoch_started_at(epoch) == at
        assert store.modes.impression_counts(at, before=at + 2) == {mode: 1}
        store.modes.log_impressions(epoch, [('wheelhouse', mode, 'cosine'), ('explore', mode, 'cosine')], at + 2)
        assert store.modes.impression_counts(at, before=at + 3) == {mode: 2}
        assert tuple(conn.execute(f'SELECT ranker,created_at FROM {mode_offers.VIEW} WHERE epoch=? AND lane=?',
                                  (epoch, 'wheelhouse')).fetchone()) == ('ppr', at)
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    report_file('mode-offers-by-rotation-c223.json').write_text(json.dumps({
        'offers': len(rows), 'epochs': len({row[0] for row in rows}),
        'unresolved_mode_offers': sum(row[-1] is not None for row in rows),
        'source_table_count': source_count, 'destination_table_count': destination_count,
    }, indent=2) + '\n', encoding='utf-8')


def test_legacy_modes_keep_their_ids_payloads_and_history_on_groups(managed_library):
    from yt_playlist.core import choice_learning_state, legacy_mode_payloads, mode_offers
    from yt_playlist.core.migrations import legacy_mode_storage

    count = "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'"
    groups = 'SELECT id,external_key,legacy_mode_id,created_at FROM taste_groups'
    picks = ('SELECT id,picked_mode_id,picked_legacy_mode_id,picked_ranker,mode_picked_at '
             'FROM playlists WHERE mode_picked_at IS NOT NULL ORDER BY id')
    offers = f'SELECT * FROM {mode_offers.VIEW} ORDER BY epoch,lane'
    payload_fields = ','.join(('mode_id', *legacy_mode_payloads.PAYLOAD_FIELDS))
    with closing(Store(managed_library).conn) as conn:
        before_change = next(i for i, change in enumerate(regular.CHANGES) if change.name == legacy_mode_storage.NAME)
        regular.migrate(conn, changes=regular.CHANGES[:before_change])
        before = conn.execute(count).fetchone()[0]
        known = {row[0]: tuple(row) for row in conn.execute(groups)}
        mapped_ids = {row[2] for row in known.values() if row[2] is not None}
        legacy = list(map(tuple, conn.execute(f'SELECT {payload_fields} FROM taste_modes ORDER BY mode_id')))
        assert legacy and mapped_ids.intersection(row[0] for row in legacy)
        assert any(row[0] not in mapped_ids for row in legacy)
        before_picks = list(map(tuple, conn.execute(picks)))
        before_offers = list(map(tuple, conn.execute(offers)))
        responses = dict(conn.execute('SELECT kind,count(*) FROM rec_response_events GROUP BY kind'))
        descriptor_stats = dict(conn.execute(f'''WITH menu AS (
            SELECT o.value->>2 AS id FROM mix_choice_menus m,json_each(m.options) o
        ), learned AS ({choice_learning_state.DESCRIPTORS})
        SELECT 'stored',count(*) FROM mix_choice_descriptors
        UNION ALL SELECT 'menu_options',count(*) FROM menu
        UNION ALL SELECT 'menu_descriptors',count(DISTINCT id) FROM menu
        UNION ALL SELECT 'learned_without_menu',count(*) FROM (SELECT * FROM learned EXCEPT SELECT id FROM menu)'''))
        regular.migrate(conn, changes=regular.CHANGES[:before_change + 1])
        assert conn.execute(count).fetchone()[0] == before - 1

    store = Store(managed_library)
    with closing(store.conn) as conn:
        store.init_schema()
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='taste_modes'").fetchone()
        assert list(map(tuple, conn.execute(f'SELECT {payload_fields} FROM {legacy_mode_payloads.VIEW} ORDER BY mode_id'))) == legacy
        restored = {row[0]: tuple(row) for row in conn.execute(groups)}
        assert all(restored[identity] == row for identity, row in known.items())
        restored_ids = {row[2]: row for row in restored.values() if row[2] is not None}
        for mode_id, *payload in legacy:
            if mode_id not in mapped_ids:
                assert restored_ids[mode_id][1] == f'legacy:{mode_id}'
        assert list(map(tuple, conn.execute(picks))) == before_picks
        assert list(map(tuple, conn.execute(offers))) == before_offers
        mode_id = store.modes.next_mode_id()
        with conn.atomic():
            conn.execute('INSERT INTO taste_groups(external_key,legacy_mode_id,created_at) VALUES (?,?,?)',
                         (f'legacy:{mode_id}', mode_id, 10.0))
        with pytest.raises(sqlite3.IntegrityError):
            store.modes.log_pick(10000000, mode_id, 11.0)
        with pytest.raises(sqlite3.IntegrityError):
            store.modes.log_impressions(10000000, [('audit', mode_id)], 11.0)
        store.modes.replace_modes([dict(mode_id=mode_id, label='Audit mode', families=[],
                                      centroid=[0.25, 0.75], size=2, rep_keys=[], space='audit')], [], 12.0)
        assert conn.execute('SELECT created_at,mode_first_seen FROM taste_groups WHERE legacy_mode_id=?',
                            (mode_id,)).fetchone()[:] == (10.0, 12.0)
        store.modes.log_impressions(10000000, [('audit', mode_id, 'cosine')], 13.0)
        store.modes.log_pick(10000000, mode_id, 14.0, 'cosine')
        assert store.modes.impression_counts(13, before=14)[mode_id] == 1
        assert mode_id not in store.modes.pick_counts(13, before=14)
        assert store.modes.pick_counts(14, before=15)[mode_id] == 1
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
        installed = conn.execute(count).fetchone()[0]

    with closing(Store(':memory:').conn) as conn:
        from yt_playlist.core.migrations import migrate
        migrate(conn)
        assert conn.execute(count).fetchone()[0] == installed
    unmatched = sum(row[0] not in mapped_ids for row in legacy)
    report_file('legacy-mode-group-ownership.json').write_text(json.dumps({
        'mapped_legacy_modes': len(legacy) - unmatched, 'previously_unmatched_modes': unmatched,
        'retained_offers': len(before_offers), 'retained_picks': len(before_picks),
        'response_kinds': responses, 'descriptors': descriptor_stats,
        'source_tables': before, 'destination_tables': installed,
    }, indent=2) + '\n', encoding='utf-8')
