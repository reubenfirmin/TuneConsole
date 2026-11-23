"""Verify active, selected and expired artist-credit captures on a managed upgrade."""
from contextlib import closing
import json

from tests.library_image.fixtures import report_file
from yt_playlist.core import artist_credits
from yt_playlist.core.migrations import regular
from yt_playlist.core.store import Store


def test_artist_credit_replays_survive_upgrade(managed_library):
    store = Store(managed_library)
    with closing(store.conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:203])
        active_sql = ('SELECT id,entry_id,credit_id,source,delivery_key,first_seen,last_seen,processing_count,'
                      'capture_kind,metadata_delivery_id,is_capture_head,is_selected FROM artist_credit_observations')
        active = list(map(tuple, conn.execute(active_sql + ' ORDER BY id')))
        assert active
        row = conn.execute('SELECT entry_id,credit_id,source FROM artist_credit_replays LIMIT 1').fetchone()
        assert row
        detached = conn.execute('SELECT MAX(id) FROM artist_credit_observations').fetchone()[0] + 1
        with conn.atomic():
            conn.execute('INSERT INTO artist_credit_replays(entry_id,credit_id,source,delivery_key,observation_id,metadata_bound) '
                         "VALUES (?,?,?,'check-migrated-expired-credit',?,0)", (*row, detached))
        expected = list(map(tuple, conn.execute('SELECT observation_id,entry_id,credit_id,source,delivery_key,metadata_bound '
                                               'FROM artist_credit_replays ORDER BY observation_id')))
        store.init_schema()

    store = Store(managed_library)
    with closing(store.conn) as conn:
        store.init_schema()
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='artist_credit_replays'").fetchone()
        assert list(map(tuple, conn.execute(active_sql + ' WHERE retired=0 ORDER BY id'))) == active
        replay = ('SELECT id,entry_id,credit_id,source,delivery_key,metadata_bound FROM artist_credit_observations '
                  'WHERE entry_id=? AND source=? AND delivery_key=?')
        for saved in expected:
            assert tuple(conn.execute(replay, (saved[1], saved[3], saved[4])).fetchone()) == saved
        lookup = [result[3] for result in conn.execute('EXPLAIN QUERY PLAN ' + replay, (row[0], row[2], 'any'))]
        assert any('SEARCH' in line and 'ix_credit_delivery' in line for line in lookup), lookup
        with conn.atomic():
            assert artist_credits.capture(conn, row[0], row[1], row[2], 1,
                                          delivery_key='check-migrated-expired-credit') == (detached, False)
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    report_file('artist-credit-replays-c204.json').write_text(json.dumps({
        'active_observations': len(active), 'existing_replays': len(expected) - 1,
        'detached_replay_checked': detached, 'lookup': lookup,
    }, indent=2) + '\n', encoding='utf-8')
