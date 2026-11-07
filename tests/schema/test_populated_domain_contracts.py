"""Opt-in domain cutover against our retained isolated populated library."""
import json
from pathlib import Path
import sqlite3
import time

import pytest

from tests.schema.test_populated_catalog_writes import signatures
from yt_playlist.core.migrations import current
from yt_playlist.core.migrations import v104_domain_contracts as domains
from yt_playlist.core.store import Store


def test_populated_domain_upgrade_preserves_all_rows_and_published_results(request,tmp_path):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply our retained isolated discovery-subjects.db')
    target = Path(option).resolve(strict=True)
    assert target.is_relative_to(Path(request.config.rootpath).resolve()/'.pytest_cache')
    assert target.name=='discovery-subjects.db'
    store = Store(target)
    try:
        version = store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
        assert 103<=version<=current.VERSION
        domains.preflight(store.conn)
        tables = [row[0] for row in store.conn.execute("SELECT name FROM sqlite_schema WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name<>'schema_migrations' ORDER BY name")]
        columns = {table:[row[1] for row in store.conn.execute('PRAGMA table_info('+table+')')] for table in tables}
        before = signatures(store.conn,tables,column_orders=columns)
        sequences = dict(store.conn.execute('SELECT name,seq FROM sqlite_sequence'))
        dependent = dict(store.conn.execute("SELECT name,sql FROM sqlite_schema WHERE type IN ('view','trigger')"))
        vibes = dict(store.conn.execute("SELECT name,sql FROM sqlite_schema WHERE name IN "
            "('sample_attributes','measurements','measurement_samples','predictions','prediction_artists','prediction_inputs','recording_vibes')"))
        catalog = [tuple(row) for row in store.conn.execute('SELECT * FROM catalog_tracks ORDER BY id')]
        playlist = [tuple(row) for row in store.conn.execute('SELECT * FROM playlist_tracks ORDER BY playlist_id,position')]
        # Persist the source evidence before DDL so interrupted verification can
        # compare named values even if canonical fresh column order differs.
        source = {'protected':before,'columns':columns,'sequences':sequences,'dependent':dependent,'vibes':vibes,
                  'catalog':catalog,'playlist':playlist,'previous_version':version}
        (tmp_path/'domain-contracts-source.json').write_text(json.dumps(source)+'\n')
        started = time.monotonic()
        store.init_schema()
        elapsed = time.monotonic()-started
        assert signatures(store.conn,tables,column_orders=columns)==before
        assert dict(store.conn.execute('SELECT name,seq FROM sqlite_sequence'))==sequences
        installed = dict(store.conn.execute("SELECT name,sql FROM sqlite_schema WHERE type IN ('view','trigger')"))
        assert {name:installed[name] for name in dependent}==dependent
        prior = current._schema_contract(current.statements(version=version))
        added = {name for name,(kind,_) in current.contract().items()
                 if kind in ('view','trigger') and name not in prior}
        assert installed.keys()-dependent.keys()==added
        assert dict(store.conn.execute("SELECT name,sql FROM sqlite_schema WHERE name IN "
            "('sample_attributes','measurements','measurement_samples','predictions','prediction_artists','prediction_inputs','recording_vibes')"))==vibes
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM catalog_tracks ORDER BY id')]==catalog
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM playlist_tracks ORDER BY playlist_id,position')]==playlist
        current.validate(store.conn)
        domains.preflight(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        for table,fields in domains.CONTRACTS.items():
            definition = store.conn.execute('SELECT sql FROM sqlite_schema WHERE name=?',(table,)).fetchone()[0]
            assert all('ck_'+table+'_'+field in definition for field in fields)
        report = {'sqlite':sqlite3.sqlite_version,'previous_version':version,'current_version':current.VERSION,
                  'upgrade_seconds':elapsed,'tables':len(before),'protected_rows':sum(row['rows'] for row in before.values()),
                  'constrained_tables':len(domains.CONTRACTS),'field_contracts':sum(map(len,domains.CONTRACTS.values())),
                  'protected':before,'catalog_rows':len(catalog),'playlist_rows':len(playlist)}
        (tmp_path/'domain-contracts-report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({key:value for key,value in report.items() if key!='protected'},sort_keys=True))
    finally:
        store.conn.close()


def test_completed_upgrade_matches_retained_v103_full_row_baseline(request,tmp_path):
    """Resume the first populated run, whose SELECT-star check found only order.

    The saved #492 report is the previously retained full-row v103 source.
    Its playlist column order comes from the retained installed predecessor;
    all other table orders matched directly in the original upgrade run.
    """
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply our retained isolated discovery-subjects.db')
    target = Path(option).resolve(strict=True)
    assert target.is_relative_to(Path(request.config.rootpath).resolve()/'.pytest_cache')
    assert target.name=='discovery-subjects.db'
    baseline = Path('/tmp/tuneconsole-492-populated/test_populated_name_cutover_pr0/entity-names-report.json')
    if not baseline.exists():
        pytest.skip('This continuation requires the retained v103 author report')
    original = json.loads(baseline.read_text())
    assert original['previous_version']==102 and original['current_version']==103
    store = Store(target)
    started = time.monotonic()
    try:
        version = store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
        assert 104<=version<=current.VERSION
        tables = sorted(original['protected'])
        columns = {table:[row[1] for row in store.conn.execute('PRAGMA table_info('+table+')')] for table in tables}
        columns['playlists'] = ['id','account_id','provider_playlist_id','title','track_count','content_hash',
            'first_seen','last_seen','last_changed','thumbnail','created_at','created_at_source','playlist_scope_id']
        assert signatures(store.conn,tables,column_orders=columns)==original['protected']
        current._validate(store.conn,current._schema_contract(current.statements(version=version)),version)
        domains.preflight(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        # Programs and the agreed seven vibe contracts still match v103.
        expected = current._schema_contract(current.statements(version=103))
        for name,(kind,definition) in expected.items():
            if kind in ('view','trigger'):
                actual = store.conn.execute('SELECT sql FROM sqlite_schema WHERE name=?',(name,)).fetchone()[0]
                assert current._tokens(actual)==definition
            elif name in {
                    'sample_attributes','measurements','measurement_samples','predictions',
                    'prediction_artists','prediction_inputs','recording_vibes'}:
                actual = store.conn.execute('SELECT sql FROM sqlite_schema WHERE name=?',(name,)).fetchone()[0]
                assert current._table(store.conn,name,actual)==definition
        assert store.conn.execute('SELECT count(*) FROM catalog_tracks').fetchone()[0]==original['catalog_rows']
        assert store.conn.execute('SELECT count(*) FROM playlist_tracks').fetchone()[0]==original['playlist_rows']
        report = {'previous_version':103,'current_version':version,'baseline':str(baseline),
                  'tables':len(tables),'protected_rows':original['protected_rows'],'protected':original['protected'],
                  'constrained_tables':len(domains.CONTRACTS),'field_contracts':sum(map(len,domains.CONTRACTS.values())),
                  'sqlite':sqlite3.sqlite_version,'validation_seconds':time.monotonic()-started,
                  'upgrade_seconds':None,'catalog_rows':original['catalog_rows'],'playlist_rows':original['playlist_rows'],
                  'note':'Initial ordinary upgrade completed; its raw SELECT-star comparison differed only in playlist column order. This continuation compares named values to the saved v103 full-row baseline.'}
        (tmp_path/'domain-contracts-report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({key:value for key,value in report.items() if key!='protected'},sort_keys=True))
    finally:
        store.conn.close()
