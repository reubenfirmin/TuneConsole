"""Opt-in naming cutover on our retained, isolated populated library."""
import json
from pathlib import Path
import sqlite3
import time

import pytest

from tests.schema.test_populated_catalog_writes import signatures
from yt_playlist.core.migrations import current
from yt_playlist.core.migrations.v103_entity_names import COLUMNS
from yt_playlist.core.store import Store


def test_populated_name_cutover_preserves_every_table_and_owner(request,tmp_path):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply our retained isolated discovery-subjects.db')
    target = Path(option).resolve(strict=True)
    assert target.is_relative_to(Path(request.config.rootpath).resolve()/'.pytest_cache')
    assert target.name=='discovery-subjects.db'
    store = Store(target)
    try:
        version = store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
        assert version in (102,103)
        tables = [row[0] for row in store.conn.execute("SELECT name FROM sqlite_schema WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name<>'schema_migrations' ORDER BY name")]
        before = {('accounts' if name=='identities' else name):value
                  for name,value in signatures(store.conn,tables).items()}
        sequences = dict(store.conn.execute('SELECT name,seq FROM sqlite_sequence'))
        catalog = [tuple(row) for row in store.conn.execute('SELECT * FROM catalog_tracks ORDER BY id')]
        playlists = [tuple(row) for row in store.conn.execute('SELECT * FROM playlist_tracks ORDER BY playlist_id,position')]
        started = time.monotonic()
        store.init_schema()
        elapsed = time.monotonic()-started
        assert signatures(store.conn,sorted(before))==before
        assert dict(store.conn.execute('SELECT name,seq FROM sqlite_sequence'))==sequences
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM catalog_tracks ORDER BY id')]==catalog
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM playlist_tracks ORDER BY playlist_id,position')]==playlists
        current.validate(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        for table,renames in COLUMNS.items():
            names = {row[1] for row in store.conn.execute('PRAGMA table_info('+table+')')}
            assert not names & renames.keys()
            assert set(renames.values())<=names
        accounts = store.accounts.get_accounts()
        assert accounts and sum(account.is_master for account in accounts)==1
        assert len(store.playlists.get_playlists())==before['playlists']['rows']
        # Exercise SQL through current repositories, including quarantine joins.
        store.overlaps.get_cleanup_ignored()
        store.overlaps.get_suppressed_overlaps()
        store.overlaps.get_overlap_kept_pairs()
        report = {'sqlite':sqlite3.sqlite_version,'previous_version':version,'current_version':103,
                  'upgrade_seconds':elapsed,'tables':len(before),'protected_rows':sum(value['rows'] for value in before.values()),
                  'protected':before,'accounts':len(accounts),'catalog_rows':len(catalog),'playlist_rows':len(playlists)}
        (tmp_path/'entity-names-report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({key:value for key,value in report.items() if key!='protected'},sort_keys=True))
    finally:
        store.conn.close()
