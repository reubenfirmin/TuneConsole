"""Continue ingestion through the normal application on an isolated v74 library."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
from threading import Barrier
import time

from tests.schema.test_populated_groups import fingerprint, populated
from tests.schema.populated_state_owners import account_boundaries, retained_originals, scope_and_account_state
from yt_playlist.core import artist_credits, artists
from yt_playlist.core.migrations import current
from yt_playlist.core.store import Store


def test_populated_credit_changes_replay_restart_and_concurrent_delivery(populated, request):
    _, target, output = populated
    donor_path = Path(request.config.getoption('--schema-upgraded-db')).resolve(strict=True)
    donor_digest = fingerprint(donor_path)
    donor = sqlite3.connect(donor_path.as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        assert donor.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 74
        maxima = {table: donor.execute(f'SELECT max(id) FROM {table}').fetchone()[0]
                  for table in ('artist_credits', 'artist_credit_observations')}
        old_columns = {table: [r[1] for r in donor.execute(f'PRAGMA table_info({table})')]
                       for table in ('artist_credits', 'artist_credit_members', 'artist_credit_observations')}
    finally:
        donor.close()
    db_path = Path(target.execute('PRAGMA database_list').fetchone()[2])
    store = Store(db_path)
    verification = None
    report = {'donor': str(donor_path), 'donor_sha256': donor_digest,
              'sqlite_version': sqlite3.sqlite_version, 'controlled_provider_changes': True}
    try:
        started = time.monotonic()
        # This is the production startup path, with no injected migration registry.
        store.init_schema()
        report['startup_seconds'] = time.monotonic() - started
        conn = store.conn
        # A normal Store connection does not enable URI parsing for ATTACH.
        # Compare through a separate reader that opens both files read-only.
        verification = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)
        verification.row_factory = sqlite3.Row
        verification.execute('ATTACH DATABASE ? AS original', (donor_path.as_uri() + '?mode=ro&immutable=1',))
        # Share this expensive disposable copy with independent all-row checks
        # of the intervening owners. Each records its own bounded evidence.
        for name, verify, reader in (('retained-originals', retained_originals, verification),
                                    ('scope-account-state', scope_and_account_state, verification),
                                    ('account-boundaries', account_boundaries, conn)):
            (output / (name + '.json')).write_text(json.dumps(verify(reader), indent=2) + '\n')
        assert verification.execute('SELECT * FROM original.track_artists EXCEPT SELECT * FROM entry_artists LIMIT 1').fetchone() is None
        assert verification.execute('SELECT * FROM entry_artists EXCEPT SELECT * FROM original.track_artists LIMIT 1').fetchone() is None
        entry_id = conn.execute('SELECT entry_id FROM entry_artist_selections ORDER BY entry_id LIMIT 1').fetchone()[0]

        def selected():
            return conn.execute('SELECT o.* FROM entry_artist_selections s '
                'JOIN artist_credit_observations o ON o.id=s.observation_id WHERE s.entry_id=?', (entry_id,)).fetchone()

        def deliver(members, revision, *, source='schema489-populated', now=1793260100):
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                artists.observe(conn, entry_id, ', '.join(m['name'] for m in members), members,
                                source=source, now=now, delivery_key=revision)
            row = selected()
            value = artist_credits.read(conn, row['credit_id'])
            assert [r['credit'] for r in store.artists.track_credits(entry_id)] == [m.credit_text for m in value.members]
            return row

        first = [{'name': 'Schema489 First', 'id': 'schema489-first', 'role': 'primary'},
                 {'name': 'Schema489 Second', 'browseId': 'schema489-second', 'role': 'guest'}]
        a = deliver(first, 'revision:1')
        reverse = list(reversed(first))
        b = deliver(reverse, 'revision:2', now=1793260101)
        assert a['credit_id'] != b['credit_id']
        assert [m.credit_text for m in artist_credits.read(conn, b['credit_id']).members] == [m['name'] for m in reverse]
        changed = [dict(reverse[0], browseId='schema489-second-new'), reverse[1]]
        c = deliver(changed, 'revision:3', now=1793260102)
        assert c['credit_id'] != b['credit_id']
        assert artist_credits.read(conn, c['credit_id']).members[0].raw_browse_id == 'schema489-second-new'
        other = deliver(changed, 'revision:1', source='schema489-independent', now=1793260103)
        assert other['id'] != c['id'] and other['source'] == 'schema489-independent'
        assert deliver(first, 'revision:1', now=1793260104)['id'] == other['id']
        assert conn.execute('SELECT processing_count FROM artist_credit_observations WHERE id=?', (a['id'],)).fetchone()[0] == 2

        statements = []
        conn.set_trace_callback(statements.append)
        try:
            for n in range(3):
                assert deliver(changed, 'revision:1', source='schema489-independent', now=1793260110+n)['id'] == other['id']
        finally:
            conn.set_trace_callback(None)
        writes = [sql for sql in statements if sql.lstrip().split(None, 1)[0].upper() in ('INSERT', 'UPDATE', 'DELETE')]
        assert not [sql for sql in writes if 'entry_artists' in sql.lower()]
        report['unchanged_projection_writes'] = 0

        # Compare every original row and column, not just capture totals. Only
        # the controlled new subjects above have been changed by this journey.
        for table, columns in old_columns.items():
            key = 'credit_id' if table == 'artist_credit_members' else 'id'
            maximum = maxima['artist_credits' if table == 'artist_credit_members' else table]
            fields = ','.join(columns)
            old = f'SELECT {fields} FROM original.{table}'
            new = f'SELECT {fields} FROM main.{table} WHERE {key}<={maximum}'
            assert verification.execute(f'{old} EXCEPT {new} LIMIT 1').fetchone() is None
            assert verification.execute(f'{new} EXCEPT {old} LIMIT 1').fetchone() is None
        report['immutable_original_rows_verified'] = True
        expected = other['id']
        store.conn.close()
        store = Store(db_path)
        store.init_schema()
        conn = store.conn
        assert selected()['id'] == expected

        gate = Barrier(2)
        def ingest(index):
            opened = Store(db_path)
            try:
                opened.init_schema()
                gate.wait(timeout=30)
                with opened.conn:
                    opened.conn.execute('BEGIN IMMEDIATE')
                    artists.observe(opened.conn, entry_id, 'Schema489 concurrent',
                        [{'name': 'Schema489 concurrent', 'id': 'schema489-concurrent'}],
                        source='schema489-concurrent', delivery_key='stable-provider-key', now=1793260120+index)
            finally:
                opened.conn.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(ingest, range(2)))
        captured = conn.execute("SELECT * FROM artist_credit_observations WHERE source='schema489-concurrent'").fetchall()
        assert len(captured) == 1 and captured[0]['processing_count'] == 2
        assert (captured[0]['first_seen'], captured[0]['last_seen']) == (1793260120, 1793260121)
        assert selected()['id'] == captured[0]['id']
        assert [r['credit'] for r in store.artists.track_credits(entry_id)] == ['Schema489 concurrent']
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
        report.update(version=current.VERSION, concurrent_captures=len(captured),
                      concurrent_processing_count=captured[0]['processing_count'],
                      older_replay_preserved_selection=True, reopen_preserved_selection=True,
                      foreign_key_violations=0)
    finally:
        store.conn.close()
        if verification is not None:
            verification.close()
        assert fingerprint(donor_path) == donor_digest, 'The retained v74 donor changed'
    (output / 'artist-credit-journeys.json').write_text(json.dumps(report, indent=2) + '\n')
