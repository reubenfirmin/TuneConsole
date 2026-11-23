"""Compare saved Radio subjects, scoped origins, and discovery fetch marks."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.library_image.fixtures import managed_database, report_file
from yt_playlist.core.recommendation_outputs import RADIO_SEED_SETTING
from yt_playlist.core.migrations.radio_discovery_details import RETIRED
from yt_playlist.core.radio_provenance import UNRESOLVED
from yt_playlist.core.store import Store


def rows(conn, query, arguments=()):
    return list(map(tuple, conn.execute(query, arguments)))


def tables(conn):
    return {name for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def test_installed_radio_and_discovery_details():
    report = {}
    path = managed_database(report=report)
    if path is None:
        pytest.skip('Requires a private managed C175 snapshot')
    with closing(sqlite3.connect(path)) as previous:
        before = tables(previous)
        seeds = rows(previous, 'SELECT position,video_id,entry_id,resolution_reason FROM radio_seed_subjects ORDER BY position')
        scopes = rows(previous, 'SELECT o.playlist_scope_id,s.provider_playlist_id,s.account_id '
            'FROM radio_playlist_origins o JOIN playlist_scopes s ON s.id=o.playlist_scope_id ORDER BY 1')
        unresolved = rows(previous, 'SELECT unresolved_provider_id FROM radio_playlist_origins '
                          'WHERE unresolved_provider_id IS NOT NULL ORDER BY 1')
        fetches = rows(previous, 'SELECT source_type,source_id,album_id FROM discovery_fetch_history ORDER BY 1,2')
        assert seeds and scopes and fetches

    store = Store(path)
    with closing(store.conn) as conn:
        store.init_schema()
        after = tables(conn)
        assert set(RETIRED) <= before - after and not after - before
        assert rows(conn, "SELECT s.value->>'position',s.value->>'video_id',s.value->>'entry_id',s.value->>'resolution_reason' "
                    "FROM settings o,json_each(o.value,'$.seed_subjects') s WHERE o.key=? ORDER BY s.key",
                    (RADIO_SEED_SETTING,)) == seeds
        assert rows(conn, 'SELECT id,provider_playlist_id,account_id FROM playlist_scopes WHERE radio_origin=1 ORDER BY 1') == scopes
        assert rows(conn, 'SELECT u.value FROM settings s,json_each(s.value) u WHERE s.key=? ORDER BY 1', (UNRESOLVED,)) == unresolved
        assert rows(conn, 'SELECT f.value->>0,f.value->>1,o.album_id FROM discovery_origins o,json_each(o.fetch_sources) f ORDER BY 1,2') == fetches
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    store = Store(path)
    with closing(store.conn):
        store.init_schema()
        seed = json.loads(store.settings.get_setting('radio_seed_offer'))
        assert [track['video_id'] for track in seed['tracks']] == [row[1] for row in seeds]
        assert set(store.playlists.radio_playlist_ids()) == {row[1] for row in scopes} | {row[0] for row in unresolved}
        album_sources = [source for kind, source, _ in fetches if kind == 'album']
        assert store.discovery.fetched_albums(album_sources) == set(album_sources)
    report.update(tables_before=len(before), tables_after=len(after), removed=sorted(before - after),
        exact_seed_subjects=len(seeds), exact_playlist_scopes=len(scopes), unresolved_provider_ids=len(unresolved),
        exact_fetch_sources=len(fetches), reopened=True, foreign_key_errors=0)
    report_file('radio-discovery-c185.json').write_text(json.dumps(report, indent=2) + '\n')
