"""Check the field-credit cutover on a private installed-library snapshot."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.library_image.fixtures import managed_database, report_file
from yt_playlist.core import metadata_runtime_files
from yt_playlist.core.migrations.provider_field_storage import OWNERS, RETIRED
from yt_playlist.core.store import Store


def rows(conn, query):
    return list(map(tuple, conn.execute(query)))


def tables(conn):
    return {name for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def test_installed_provider_fields_and_current_state():
    report = {}
    path = managed_database(report=report)
    if path is None:
        pytest.skip('Requires a private managed C175 snapshot')
    with closing(sqlite3.connect(path)) as previous:
        before = tables(previous)
        queries = {table: f'SELECT {",".join(row[1] for row in previous.execute(f"PRAGMA table_info({table})"))} '
                          f'FROM {table} ORDER BY 1,2' for table in OWNERS}
        owners = {table: rows(previous, query) for table, query in queries.items()}
        attempts = rows(previous, 'SELECT attempt_id,field,observation_id FROM provider_attempt_fields ORDER BY 1,2')
        state = rows(previous, 'SELECT recording_id,provider,field,delivery_id,observation_id FROM provider_state_fields ORDER BY 1,2,3')
        owner, revision = previous.execute("SELECT owner_key,revision_key FROM metadata_file_writes "
            "WHERE kind='alternatives' AND file_status='ready' AND selected=1 LIMIT 1").fetchone()

    store = Store(path)
    with closing(store.conn) as conn:
        store.init_schema()
        after = tables(conn)
        assert set(RETIRED) <= before - after and not after - before
        assert {table: rows(conn, query) for table, query in queries.items()} == owners
        assert rows(conn, 'SELECT a.id,f.key,f.atom FROM provider_attempts a,json_each(a.field_links) f ORDER BY 1,2') == attempts
        assert rows(conn, 'SELECT s.recording_id,s.provider,f.key,f.value->>0,f.value->>1 '
                          'FROM enrichment_provider_state s,json_each(s.field_links) f ORDER BY 1,2,3') == state
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    store = Store(path)
    with closing(store.conn) as conn:
        store.init_schema()
        assert metadata_runtime_files.read(conn, 'alternatives', tuple(json.loads(owner)), float.fromhex(revision))
    report.update(tables_before=len(before), tables_after=len(after), removed=sorted(before - after),
        preserved_rows={table: len(values) for table, values in owners.items()},
        attempt_field_links=len(attempts), current_field_links=len(state), selected_alternatives_read=True,
        reopened=True, foreign_key_errors=0)
    report_file('provider-field-links-c183.json').write_text(json.dumps(report, indent=2) + '\n')
