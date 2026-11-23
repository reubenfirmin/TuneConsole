"""Check a populated TV upgrade in a private library workspace."""
from contextlib import closing
import json

from tests.library_image.fixtures import report_file
from yt_playlist.core import append_work, collection_recovery
from yt_playlist.core.migrations import regular, tv_storage
from yt_playlist.core.store import Store


TV_ID = 'tvAudit0001'
SECOND_ID = 'tvAudit0002'


def _tables(conn):
    return {name for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name GLOB 'tv_*'")}


def _seed_tv(conn):
    account = conn.execute('SELECT id FROM accounts ORDER BY id LIMIT 1').fetchone()[0]
    recording = conn.execute('SELECT id,identity_key FROM recordings ORDER BY id LIMIT 1').fetchone()
    artist = conn.execute('SELECT id FROM artists ORDER BY id LIMIT 1').fetchone()[0]
    scope = conn.execute('SELECT id FROM playlist_scopes ORDER BY id LIMIT 1').fetchone()[0]
    with conn.atomic():
        conn.execute('INSERT OR IGNORE INTO tv_pool_state(id,field_mask) VALUES(1,0)')
        conn.execute('UPDATE tv_pool_state SET field_mask=field_mask|24 WHERE id=1')
        conn.execute('DELETE FROM tv_pool_items WHERE position=0')
        conn.execute('DELETE FROM tv_pool_attempts WHERE position=0 OR raw_key=?', (recording[1],))
        conn.execute("INSERT INTO tv_pool_items(pool_id,position,field_mask,video_id,source_key,source_recording_id,"
                     "video_resolution) VALUES(1,0,65,?,?,?,'external_video')", (TV_ID, recording[1], recording[0]))
        conn.execute('INSERT INTO tv_pool_attempts VALUES(1,0,?,?,NULL)', (recording[1], recording[0]))
        conn.execute('INSERT OR IGNORE INTO tv_artwork_state VALUES(1,10)')
        conn.execute('INSERT OR IGNORE INTO tv_artwork_request_state VALUES(1,11)')
        conn.execute('INSERT OR IGNORE INTO tv_visual_state VALUES(1,12)')
        art = conn.execute("INSERT INTO tv_artist_artwork(artist_key,artist_id,field_mask) VALUES('audit-tv',?,1) "
                           'RETURNING id', (artist,)).fetchone()[0]
        conn.executemany('INSERT INTO tv_artist_images VALUES(?,?,?)', [(art, 0, 'image-z'), (art, 1, 'image-a')])
        visual = conn.execute('INSERT INTO tv_visual_results(video_id,field_mask) VALUES(?,64) RETURNING id',
                              (TV_ID,)).fetchone()[0]
        conn.executemany('INSERT INTO tv_visual_samples VALUES(?,?,?)', [(visual, 0, 15), (visual, 1, 20)])
        conn.execute('INSERT INTO tv_launches(request_id,account_id,owner_id,stream,current,created_at) '
                     "VALUES('audit-tv',?,?,?,0,30)", (account, account, 'a' * 32))
        conn.executemany("INSERT INTO tv_launch_videos(request_id,position,video_id,title,source_key,write_state) "
                         "VALUES('audit-tv',?,?,?,?,?)", [(0, TV_ID, 'First', recording[1], 'confirmed'),
                                                           (1, SECOND_ID, 'Second', recording[1], 'uncertain')])
        append_id = conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM tv_append_intents').fetchone()[0]
        payload = {'request_id': 'audit-tv', 'state': 'pending', 'before': [TV_ID, TV_ID],
                   'additions': [{'video_id': SECOND_ID}]}
        reference = collection_recovery.publish(conn, 'tv-append', append_id, payload)
        conn.execute("INSERT INTO tv_append_intents VALUES(?,NULL,?,'audit-stream','audit-tv','pending',30,?,?,?)",
                     (append_id, scope, *reference))
        conn.executemany('INSERT INTO tv_append_items VALUES(?,?,?,?)',
                         [(append_id, 'before', 0, TV_ID), (append_id, 'before', 1, TV_ID),
                          (append_id, 'additions', 0, SECOND_ID)])
    return tuple(recording), append_id, reference


def test_actual_tv_upgrade_keeps_queue_provenance_and_retry(managed_library):
    store = Store(managed_library)
    with closing(store.conn) as conn:
        change = next(change for change in regular.CHANGES if change.name == tv_storage.NAME)
        regular.migrate(conn, changes=regular.CHANGES[:change.version - 1])
        before = _tables(conn)
        recording, append_id, reference = _seed_tv(conn)
        draw = tuple(conn.execute('SELECT draw_number,recent_videos,last_videos FROM tv_preparation_state').fetchone() or
                     (0, '[]', '[]'))
        regular.migrate(conn, changes=regular.CHANGES[:change.version])
        after = _tables(conn)
        assert before - after == set(tv_storage.RETIRED)
        assert after <= before and (len(before), len(after)) == (18, 9)
        assert tuple(conn.execute('SELECT draw_number,recent_videos,last_videos FROM tv_preparation_state').fetchone()) == draw
        candidate = conn.execute("SELECT video_id,source_key,source_recording_id FROM tv_pool_items "
                                 "WHERE role='candidate' AND position=0").fetchone()
        assert tuple(candidate) == (TV_ID, recording[1], recording[0])
        attempt = conn.execute("SELECT attempt_key,recording_id FROM tv_pool_items "
                               "WHERE role='attempt' AND position=0").fetchone()
        assert tuple(attempt) == (recording[1], recording[0])
        images = conn.execute("SELECT image_urls FROM tv_artist_artwork WHERE artist_key='audit-tv'").fetchone()[0]
        assert json.loads(images) == ['image-z', 'image-a']
        samples = conn.execute('SELECT first_sample_at,second_sample_at FROM tv_visual_results WHERE video_id=?', (TV_ID,))
        assert tuple(samples.fetchone()) == (15, 20)
        append = conn.execute('SELECT state,before_videos,additions_videos,recovery_filename,recovery_digest,recovery_bytes '
                              'FROM tv_append_intents WHERE id=?', (append_id,)).fetchone()
        assert append[0] == 'pending' and json.loads(append[1]) == [TV_ID, TV_ID]
        assert json.loads(append[2]) == [SECOND_ID] and tuple(append[3:]) == reference
        assert collection_recovery.read(*reference, kind='tv-append', owner=append_id)['before'] == [TV_ID, TV_ID]
        scope = conn.execute('SELECT playlist_scope_id FROM tv_append_intents WHERE id=?', (append_id,)).fetchone()[0]
        regular.migrate(conn)
        checkpoint = next(row['checkpoint'] for row in append_work.entries(conn, scope)
                          if row['stream_token'] == 'audit-stream' and row['request_id'] == 'audit-tv')
        assert checkpoint['state'] == 'pending' and checkpoint['before'] == [TV_ID, TV_ID]
        assert checkpoint['additions'] == [{'video_id': SECOND_ID}]
        store._bind_repositories()
        launch = store.tv.launch('audit-tv')
        assert launch['state'] == 'pending'
        assert [video['video_id'] for video in launch['items']] == [TV_ID, SECOND_ID]
        assert launch['outcome']['confirmed'] == [TV_ID] and launch['outcome']['uncertain'] == [SECOND_ID]
        store.tv.save_launch('audit-tv', destination=('existing-destination', 'TV'))
        store.tv.complete_launch('audit-tv', [SECOND_ID])
        assert store.tv.launch('audit-tv')['outcome']['confirmed'] == [TV_ID, SECOND_ID]
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'

    report_file('tuneconsole-1435-tv-upgrade.json').write_text(json.dumps({
        'tv_tables_before': len(before), 'tv_tables_after': len(after),
        'resumed_videos': [TV_ID, SECOND_ID], 'source_recording_id': recording[0],
    }, indent=2) + '\n', encoding='utf-8')
