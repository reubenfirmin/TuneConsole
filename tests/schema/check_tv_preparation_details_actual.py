"""Compare TV publications and exact retries on a private library upgrade."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.library_image.fixtures import managed_database, report_file
from yt_playlist.core import tv_artwork_state as artwork, tv_recommendation_state as tv
from yt_playlist.core.tv_continuations import read_all
from yt_playlist.core.migrations.tv_preparation_details import RETIRED
from yt_playlist.core.store import Store


REQUEST = '91:original:upgrade'


def rows(conn, query, parameters=()):
    return list(map(tuple, conn.execute(query, parameters)))


def tables(conn):
    return {name for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def seed_empty_branches(conn):
    artist = conn.execute('SELECT id FROM artists ORDER BY id LIMIT 1').fetchone()[0]
    conn.execute("INSERT INTO tv_artist_requests(artist_key,display_name,artist_id,requested_at) "
                 "VALUES('upgrade', 'Upgrade', ?, 11.123456789012345)", (artist,))
    conn.execute('UPDATE tv_preparation_state SET artwork_requests_published=1 WHERE id=1')
    receipt = conn.execute("INSERT INTO tv_continuation_receipts(request_key,source_playlist_id,source_resolution,"
        "input_stream,request_id,status,target_playlist_id,target_resolution,target_stream,built_at) "
        "VALUES (?,91,'missing_playlist','original','upgrade','complete',92,'missing_playlist','next',10) RETURNING id",
        (REQUEST,)).fetchone()[0]
    conn.executemany("INSERT INTO tv_continuation_items(receipt_id,position,field_mask,video_id,title,"
        "source_resolution,video_resolution) VALUES (?,?,3,?,?,'missing_recording','external_video')",
        [(receipt, 2, 'tvAudit0002', None), (receipt, 5, 'tvAudit0001', 'First')])
    conn.commit()


def test_installed_tv_preparation_details():
    report = {}
    path = managed_database(report=report)
    if path is None:
        pytest.skip('Requires a private managed C175 snapshot')
    with closing(sqlite3.connect(path)) as old:
        before = tables(old)
        installed_counts = {name: old.execute('SELECT count(*) FROM ' + name).fetchone()[0] for name in RETIRED}
        seed_empty_branches(old)
        common = 'position,video_id,source_recording_id,source_resolution,recording_id,'
        candidates = rows(old, 'SELECT ' + common + "resolution_reason FROM tv_pool_items WHERE role='candidate' ORDER BY position")
        candidate_values = [tv._view(mask, tv.ITEM, values) for mask, *values in rows(old,
            'SELECT field_mask,video_id,title,artist,thumbnail,duration,kind,source_key,source_artist,'
            "source_group,connection,key FROM tv_pool_items WHERE role='candidate' ORDER BY position")]
        attempts = rows(old, 'SELECT position,attempt_key,recording_id,resolution_reason FROM tv_pool_items '
                        "WHERE role='attempt' ORDER BY position")
        items = rows(old, 'SELECT request_key,i.position,i.video_id,i.source_recording_id,i.source_resolution,'
                     'i.recording_id,i.video_resolution FROM tv_continuation_items i '
                     'JOIN tv_continuation_receipts ON receipt_id=id ORDER BY request_key,position')
        demand = rows(old, 'SELECT artist_key,display_name,artist_id,requested_at,resolution_reason FROM tv_artist_requests ORDER BY id')
        art_fields = 'artist_key,artist_id,resolution_reason,field_mask,retry_at,at,image_urls'
        art = rows(old, 'SELECT ' + art_fields + ' FROM tv_artist_artwork ORDER BY id')
        visuals = {}
        for video, mask, *values in rows(old, 'SELECT video_id,field_mask,' + ','.join(tv.VISUAL[:-1]) +
                ',first_sample_at,second_sample_at FROM tv_visual_results ORDER BY id'):
            samples = [time for time in values[-2:] if time is not None]
            visuals[video] = tv._view(mask, tv.VISUAL, (*values[:-2], samples))
    store = Store(path)
    with closing(store.conn) as conn:
        store.init_schema()
        after = tables(conn)
        assert set(RETIRED) <= before - after and not after - before
        common = ','.join("j.value->>'" + field + "'" for field in
                          ('position', 'video_id', 'source_recording_id', 'source_resolution', 'recording_id', 'video_resolution'))
        assert rows(conn, 'SELECT ' + common + " FROM settings,json_each(settings.value,'$.items') j WHERE settings.key=? ORDER BY j.key",
                    (tv.POOL_KEY,)) == candidates
        assert rows(conn, "SELECT j.value->>'position',j.value->>'key',j.value->>'recording_id',j.value->>'resolution_reason' "
                    "FROM settings,json_each(settings.value,'$.attempted') j WHERE settings.key=? ORDER BY j.key", (tv.POOL_KEY,)) == attempts
        item_fields = ('position', 'video_id', 'source_recording_id', 'source_resolution', 'recording_id', 'video_resolution')
        assert [(receipt['request_key'], *(item.get(field) for field in item_fields))
                for receipt in sorted(read_all(conn), key=lambda value: value['request_key'])
                for item in receipt['items']] == items
        state = artwork.load(conn)
        assert [(key, r['name'], r['artist_id'], r['at']) for key, r in state['requests'].items()] == [row[:4] for row in demand]
        assert [(key, r['artist_id']) for key, r in state['results'].items()] == [row[:2] for row in art]
        assert (store.rec_proposals.get_proposals(artwork.RESULTS) or {}) == {
            key: tv._view(mask, tv.ARTWORK, (json.loads(images), retry, at))
            for key, _, _, mask, retry, at, images in art}
        assert store.rec_proposals.get_proposals('tv_pool')['items'] == candidate_values
        assert store.rec_proposals.get_proposals('tv_visual_checks') == visuals
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    store = Store(path)
    with closing(store.conn):
        store.init_schema()
        assert store.tv.receipt(REQUEST) == {'playlist_id': 92, 'stream': 'next',
            'items': [{'video_id': 'tvAudit0002', 'title': None}, {'video_id': 'tvAudit0001', 'title': 'First'}]}
        assert store.rec_proposals.get_proposals('tv_artist_artwork_requests')['upgrade'] == 'Upgrade'
        store.rec_proposals.put_proposals('tv_visual_checks', {}, 12)
        assert store.rec_proposals.get_proposals('tv_visual_checks') == {}
        visual = {'verdict': 'static', 'version': 7, 'sample_times': [10.2, 12.3]}
        store.tv.publish_visual('tvAudit0001', visual, 13)
        assert store.tv.static_ids(7) == {'tvAudit0001'}
        assert store.rec_proposals.get_proposals('tv_visual_checks') == {'tvAudit0001': visual}
    report.update(tables_before=len(before), tables_after=len(after), installed_counts=installed_counts,
        exact_candidates=len(candidates), exact_attempts=len(attempts), exact_continuation_items=len(items),
        exact_artwork_results=len(art), exact_demands=len(demand), exact_visual_results=len(visuals),
        visual_cache_rebuilt=True, reopened=True, foreign_key_errors=0)
    report_file('tv-preparation-c187.json').write_text(json.dumps(report, indent=2) + '\n')
