"""Verify the control migration and restart against a managed C175 library."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.library_image.fixtures import managed_database, report_file
from yt_playlist.core import album_tuning_state, credit_retention, metadata_runtime, mode_retention
from yt_playlist.core.metadata_contract import ACTIVE, CAUGHT_UP, EXPIRY
from yt_playlist.core.recommendation_requests import read
from yt_playlist.core.store import Store
from yt_playlist.rec import album_tuning


def _tables(conn):
    return {name for name, in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def test_control_settings_upgrade():
    report = {}
    path = managed_database(report=report)
    if path is None:
        pytest.skip('Requires a private managed library snapshot')
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('PRAGMA foreign_keys=ON')
        assert conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == 175
        before = _tables(conn)
        tuning = conn.execute('SELECT expires_at,novelty,genres_json FROM album_menu_controls').fetchone()
        if tuning is None or tuning[0] is None:
            redirected = conn.execute(
                'SELECT key FROM genre_catalog WHERE redirect_key IS NOT NULL ORDER BY key LIMIT 1').fetchone()
            genre = [redirected[0], None] if redirected else [None, 'Unlisted style']
            conn.execute('''INSERT INTO album_menu_controls(singleton,expires_at,novelty,genres_json)
                VALUES (1,1000,.6,?) ON CONFLICT(singleton) DO UPDATE SET
                expires_at=excluded.expires_at,novelty=excluded.novelty,genres_json=excluded.genres_json''',
                (json.dumps([genre, [None, 'Unlisted style']]),))
        conn.execute("""UPDATE album_menu_controls SET refresh_epoch=17,
            refresh_exclusions_json='["previous","unknown"]',refresh_updated_at=42
            WHERE refresh_exclusions_json IS NULL""")
        conn.execute("""INSERT INTO model_selection_heads(singleton,schedule_next_at,schedule_saved_at)
            VALUES (1,1000,42) ON CONFLICT(singleton) DO UPDATE SET
            schedule_next_at=excluded.schedule_next_at,schedule_saved_at=excluded.schedule_saved_at
            WHERE schedule_next_at IS NULL""")
        fixture_run = conn.execute(
            """INSERT INTO rec_model_selection_runs(started_at,result_json,identity_version)
               VALUES (42,'{"kind":"daily"}',2) RETURNING id""").fetchone()[0]
        conn.execute('''UPDATE model_selection_heads SET
            active_run_id=coalesce(active_run_id,?),display_run_id=coalesce(display_run_id,?),
            restore_run_id=coalesce(restore_run_id,?)''', (fixture_run,) * 3)
        if conn.execute('SELECT 1 FROM taste_mode_retired_payloads LIMIT 1').fetchone() is None:
            mode_id = conn.execute('SELECT high_water+1 FROM taste_mode_retention').fetchone()[0]
            conn.execute("""INSERT INTO taste_modes(mode_id,label,families,centroid,size,rep_keys,active,first_seen,last_seen,space)
                VALUES (?,'Retired mode','[]',X'',0,'[]',0,1,2,'')""", (mode_id,))
            conn.execute('INSERT INTO taste_mode_retired_payloads VALUES (?,42)', (mode_id,))
        expires, novelty, genres, epoch, exclusions, refresh_at = conn.execute('''SELECT expires_at,novelty,genres_json,
            refresh_epoch,refresh_exclusions_json,refresh_updated_at FROM album_menu_controls''').fetchone()
        visible_genres = [row[0] for row in conn.execute('''SELECT
            coalesce(g.redirect_key,g.key,json_extract(c.value,'$[0]'),json_extract(c.value,'$[1]'))
            FROM json_each(?) c LEFT JOIN genre_catalog g ON g.key=json_extract(c.value,'$[0]') ORDER BY c.key''', (genres,))]
        active, displayed, restore, schedule, saved = conn.execute('''SELECT active_run_id,display_run_id,
            restore_run_id,schedule_next_at,schedule_saved_at FROM model_selection_heads''').fetchone()
        enabled, cursor, after, shard, caught = conn.execute('''SELECT active,gc_cursor,gc_after,gc_shard,
            enrichment_caught_up_at FROM metadata_runtime_state''').fetchone()
        modes = conn.execute('''SELECT enabled,cursor,next_scan_at,high_water,
            credit_enabled,credit_cursor,credit_completed,credit_next_scan_at FROM taste_mode_retention''').fetchone()
        retired = conn.execute('SELECT mode_id,retired_at FROM taste_mode_retired_payloads ORDER BY mode_id').fetchall()
        ids = conn.execute('SELECT id FROM rec_model_selection_runs ORDER BY id').fetchall()

    for restart in range(2):
        store = Store(path)
        with closing(store.conn) as conn:
            store.init_schema()
            removed = {'album_menu_controls', 'metadata_runtime_state', 'taste_mode_retention',
                       'model_selection_heads', 'taste_mode_retired_payloads'}
            tables_after = _tables(conn)
            assert removed <= before - tables_after
            assert conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0] >= 176
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()
            assert list(map(tuple, conn.execute('SELECT id FROM rec_model_selection_runs ORDER BY id'))) == ids
            assert store.settings.get_setting('rec_selection_active_run') == str(active)
            assert store.settings.get_setting('rec_selection_display_run') == str(displayed)
            assert store.settings.get_setting('rec_selection_restore') == str(restore)
            assert store.model_selection.next_daily_at() == schedule
            assert json.loads(store.settings.get_setting('daily_model_schedule'))['saved_at'] == saved
            assert album_tuning.state(store, expires - 10) == {
                'values': {'novelty': novelty}, 'genres': visible_genres, 'remaining': 10}
            assert album_tuning.state(store, expires)['remaining'] == 0
            assert read(conn, album_tuning_state.REFRESH) == (
                {'epoch': epoch, 'exclude': json.loads(exclusions)} if epoch is not None else {})
            assert json.loads(store.settings.get_setting(album_tuning_state.REFRESH))['updated_at'] == refresh_at
            assert metadata_runtime.active(conn) == bool(enabled)
            assert int(store.settings.get_setting(ACTIVE)) == enabled
            assert json.loads(store.settings.get_setting(EXPIRY)) == {
                'gc_cursor': cursor, 'gc_after': after, 'gc_shard': shard}
            assert store.enrichment_queue.caught_up_at() == caught
            assert store.settings.get_setting(CAUGHT_UP) == (str(caught) if caught is not None else None)
            assert json.loads(store.settings.get_setting(mode_retention.CHECKPOINT)) == dict(
                zip(('enabled', 'cursor', 'next_scan_at'), modes[:3], strict=True))
            assert int(store.settings.get_setting(mode_retention.HIGH_WATER)) == modes[3]
            assert credit_retention.state(conn) == dict(
                zip(('enabled', 'cursor', 'completed', 'next_scan_at'), modes[4:], strict=True))
            assert list(map(tuple, conn.execute('SELECT mode_id,payload_retired_at FROM legacy_mode_payloads '
                'WHERE payload_retired_at IS NOT NULL ORDER BY mode_id'))) == retired
    report.update(tables_before=len(before), tables_after=len(tables_after), removed=sorted(removed),
                  retired_modes=len(retired), model_runs=len(ids), reopened=True, foreign_key_errors=0)
    report_file('control-settings-c176.json').write_text(json.dumps(report, indent=2) + '\n')
