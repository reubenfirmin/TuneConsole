"""Check selection and receipt replay after upgrading a populated private library."""
from contextlib import closing
import json

import pytest

from tests.library_image.fixtures import report_file
from yt_playlist.core import collection_recovery, spotify_records
from yt_playlist.core.migrations import regular, spotify_storage
from yt_playlist.core.store import Store


def _tables(conn):
    return {name for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name GLOB 'spotify_*'")}


def _seed_spotify(conn):
    account = conn.execute('SELECT id FROM accounts ORDER BY id LIMIT 1').fetchone()[0]
    with conn.atomic():
        active = conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM spotify_import_jobs').fetchone()[0]
        for identity, label in ((active, 'active'), (active + 1, 'older report')):
            reference = collection_recovery.publish(conn, 'spotify-import', identity, {'report': {'label': label}})
            conn.execute('INSERT INTO spotify_import_jobs(id,account_id,account_present,revision,field_mask,status,'
                         "recovery_filename,recovery_digest,recovery_bytes) VALUES(?,?,1,?,0,'running',?,?,?)",
                         (identity, account, 'a' * 32, *reference))
        conn.execute('INSERT INTO spotify_import_job_head VALUES(1,?) ON CONFLICT(id) DO UPDATE SET job_id=excluded.job_id',
                     (active,))
        receipt = conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM spotify_transfer_receipts').fetchone()[0]
        slots = ['source-one', None, 'source-one', 'source-two']
        value = {'creation_phase': 'creating', 'marker': 'audit-marker', 'source_video_ids': slots}
        reference = collection_recovery.publish(conn, 'spotify-transfer', receipt, value)
        conn.execute('INSERT INTO spotify_transfer_receipts(id,source_kind,captured_account_id,account_id,fingerprint,'
                     'revision,creation_phase,marker,recovery_filename,recovery_digest,recovery_bytes) '
                     "VALUES(?,'playlist',?,?,'audit-spotify',?,'creating','audit-marker',?,?,?)",
                     (receipt, account, account, 'b' * 32, *reference))
        conn.executemany('INSERT INTO spotify_transfer_items VALUES(?,?,?)',
                         [(receipt, position, slot) for position, slot in enumerate(slots)])
    return account, active, receipt, slots, reference


def test_actual_spotify_upgrade_resumes_selected_job_and_ordered_transfer(managed_library):
    store = Store(managed_library)
    with closing(store.conn) as conn:
        change = next(change for change in regular.CHANGES if change.name == spotify_storage.NAME)
        regular.migrate(conn, changes=regular.CHANGES[:change.version - 1])
        before = _tables(conn)
        account, active, receipt, slots, reference = _seed_spotify(conn)
        regular.migrate(conn, changes=regular.CHANGES[:change.version])
        after = _tables(conn)
        assert before - after == {'spotify_import_job_head', 'spotify_transfer_items'}
        assert after <= before and 'spotify_history_listens' in after
        assert conn.execute('SELECT id FROM spotify_import_jobs WHERE is_current=1').fetchone()[0] == active
        assert conn.execute('SELECT 1 FROM spotify_import_jobs WHERE id=? AND is_current=0', (active + 1,)).fetchone()
        row = conn.execute('SELECT recovery_filename,recovery_digest,recovery_bytes FROM spotify_transfer_receipts WHERE id=?',
                           (receipt,)).fetchone()
        assert tuple(row) == reference
        regular.migrate(conn)
        assert _tables(conn) == {'spotify_history_listens'}
        assert spotify_records.read(conn, spotify_records.IMPORT + str(active + 1))['id'] == active + 1
        migrated = spotify_records.read(conn, spotify_records.transfer_key('playlist', account, 'audit-spotify'))
        assert migrated['id'] == receipt and migrated['slots'] == slots
        assert tuple(migrated[field] for field in spotify_records.REFERENCE) == reference
        store._bind_repositories()
        job = store.settings.spotify_import_job()
        assert (job['status'], job['account_id'], job['_job_revision']) == ('running', account, 'a' * 32)
        assert job['report'] == {'label': 'active'}
        store.settings.save_spotify_import_job({**job, 'status': 'done'})
        with pytest.raises(ValueError, match='replaced or updated'):
            store.settings.save_spotify_import_job(job)
        assert store.settings.spotify_import_job()['status'] == 'done'
        transfer = store.settings.spotify_transfer_receipts(account)['audit-spotify']
        assert transfer['source_video_ids'] == slots and transfer['_receipt_revision'] == 'b' * 32
        store.settings.save_spotify_transfer(account, 'audit-spotify', {
            **transfer, 'creation_phase': 'created', 'ytm_playlist_id': 'audit-spotify-target',
        })
        assert store.settings.spotify_transfer_receipts(account)['audit-spotify']['source_video_ids'] == slots
        with pytest.raises(ValueError, match='stale revision'):
            store.settings.save_spotify_transfer(account, 'audit-spotify', transfer)
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'

    report_file('tuneconsole-1433-spotify-upgrade.json').write_text(json.dumps({
        'import_tables_before': len(before - {'spotify_history_listens'}),
        'import_tables_after': len(after - {'spotify_history_listens'}),
        'retained_job': active, 'retained_receipt': receipt, 'source_slots': slots,
    }, indent=2) + '\n', encoding='utf-8')
