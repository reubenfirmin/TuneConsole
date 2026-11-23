"""Verify the recovery owner cutover against the managed library."""
from contextlib import closing
import json

import pytest

from tests.library_image.fixtures import report_file
from yt_playlist.core import append_work, collection_recovery, spotify_records, sync_state
from yt_playlist.core.migrations import regular
from yt_playlist.core.store import Store
from yt_playlist.repos.collection_recovery import references


def test_recovery_upgrade_reopens_with_confirmed_progress(managed_library):
    store = Store(managed_library)
    with closing(store.conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:194])
        account, = conn.execute('SELECT id FROM accounts ORDER BY id LIMIT 1').fetchone()
        scope, = conn.execute('SELECT id FROM playlist_scopes ORDER BY id LIMIT 1').fetchone()
        cursor_rows = [dict(row) for row in conn.execute('SELECT * FROM sync_cursors WHERE id>0 ORDER BY id')]
        attempt = dict(conn.execute('SELECT * FROM sync_cursors WHERE id=0').fetchone())
        slots = ['first', None, 'first', 'next']
        with conn.atomic():
            conn.execute("INSERT INTO tv_launches(request_id,account_id,owner_id,stream,created_at,videos) "
                         "VALUES('audit-launch-owner',?,?,?,?,?)",
                         (account, account, 'a' * 32, 200, json.dumps([
                             {'video_id': 'first', 'write_state': 'confirmed'},
                             {'video_id': 'next', 'write_state': 'uncertain'}])))
            append_id, = conn.execute('SELECT coalesce(max(id),0)+1 FROM tv_append_intents').fetchone()
            value = {'request_id': 'audit-refill-owner', 'state': 'pending', 'before': ['first', 'first'],
                     'additions': [{'video_id': 'next'}]}
            coordinate = collection_recovery.publish(conn, 'tv-append', append_id, value)
            conn.execute('INSERT INTO tv_append_intents(id,playlist_scope_id,stream_token,request_id,state,started_at,'
                         'recovery_filename,recovery_digest,recovery_bytes,before_videos,additions_videos) '
                         "VALUES(?,?,?,'audit-refill-owner','pending',200,?,?,?,?,?)",
                         (append_id, scope, 'audit-stream', *coordinate, '["first","first"]', '["next"]'))
            conn.execute('UPDATE spotify_import_jobs SET is_current=0')
            job_id, = conn.execute('SELECT coalesce(max(id),0)+1 FROM spotify_import_jobs').fetchone()
            for identity, label, current in ((job_id, 'active', 1), (job_id + 1, 'older', 0)):
                coordinate = collection_recovery.publish(conn, 'spotify-import', identity, {'report': {'label': label}})
                conn.execute('INSERT INTO spotify_import_jobs(id,account_id,account_present,revision,field_mask,status,'
                             "recovery_filename,recovery_digest,recovery_bytes,is_current) VALUES(?,?,1,?,0,'running',?,?,?,?)",
                             (identity, account, '1' * 32, *coordinate, current))
            transfer_id, = conn.execute('SELECT coalesce(max(id),0)+1 FROM spotify_transfer_receipts').fetchone()
            coordinate = collection_recovery.publish(conn, 'spotify-transfer', transfer_id, {
                'creation_phase': 'creating', 'marker': 'audit-owner', 'source_video_ids': slots})
            conn.execute('INSERT INTO spotify_transfer_receipts(id,source_kind,captured_account_id,account_id,fingerprint,'
                         'revision,creation_phase,marker,recovery_filename,recovery_digest,recovery_bytes,source_video_ids) '
                         "VALUES(?,'playlist',?,?,'audit-fingerprint',?,'creating','audit-owner',?,?,?,?)",
                         (transfer_id, account, account, '2' * 32, *coordinate, json.dumps(slots)))
        regular.migrate(conn, changes=regular.CHANGES[:197])
        migrated = sorted(sync_state.cursors(conn), key=lambda row: row['id'])
        assert [{key: row[key] for key in cursor_rows[0]} for row in migrated] == cursor_rows
        assert all(sync_state.head(conn)[key] == value for key, value in attempt.items()
                   if key not in sync_state.HISTORY_FIELDS)
        assert next(row['checkpoint'] for row in append_work.entries(conn, scope) if row['id'] == append_id) == value
        archive = {(kind, identity): filename for _, identity, kind, filename, _, _ in references(conn)}
        assert all(('spotify-import', identity) in archive for identity in (job_id, job_id + 1))
        assert archive['spotify-transfer', transfer_id] == coordinate[0]
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'

    store = Store(managed_library)
    with closing(store.conn) as conn:
        store._bind_repositories()
        old = store.sync.state()
        revision = store.sync.begin_attempt(300)
        store.sync.checkpoint(300, old, expected_revision=revision, previous=old)
        assert store.sync.state()['accounts'].keys() == old['accounts'].keys()
        launch = store.tv.launch('audit-launch-owner')
        assert launch['outcome']['confirmed'] == ['first'] and launch['outcome']['uncertain'] == ['next']
        store.tv.save_launch('audit-launch-owner', destination=('audit-tv-target', 'TV'))
        store.tv.complete_launch('audit-launch-owner', ['next'])
        assert store.tv.launch('audit-launch-owner')['outcome']['confirmed'] == ['first', 'next']
        job = store.settings.spotify_import_job()
        assert job['report'] == {'label': 'active'} and job['_job_revision'] == '1' * 32
        store.settings.save_spotify_import_job({**job, 'status': 'done'})
        with pytest.raises(ValueError, match='replaced or updated'):
            store.settings.save_spotify_import_job(job)
        transfer = store.settings.spotify_transfer_receipts(account)['audit-fingerprint']
        assert transfer['source_video_ids'] == slots and transfer['_receipt_revision'] == '2' * 32
        store.settings.save_spotify_transfer(account, 'audit-fingerprint', {
            **transfer, 'creation_phase': 'created', 'ytm_playlist_id': 'audit-spotify-target'})
        with pytest.raises(ValueError, match='stale revision'):
            store.settings.save_spotify_transfer(account, 'audit-fingerprint', transfer)
        assert store.settings.spotify_transfer_receipts(account)['audit-fingerprint']['source_video_ids'] == slots
        assert spotify_records.read(conn, spotify_records.IMPORT + str(job_id + 1))['id'] == job_id + 1

    report_file('recovery-owners-c197.json').write_text(json.dumps({
        'cursor_rows': len(cursor_rows), 'resumed_accounts': len(old['accounts']),
        'spotify_transfer_slots': slots, 'tv_launch_confirmed': ['first', 'next'],
    }, indent=2) + '\n', encoding='utf-8')
