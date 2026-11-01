"""Opt-in action upgrade against a retained actual-library donor; no remote calls."""
from pathlib import Path
import sqlite3
import time

import pytest

from tests.schema.actual_library import copy_readonly_library
from yt_playlist.core import action_backup_files, collection_recovery
from yt_playlist.core.migrations import current, migrate, registry
from yt_playlist.core.store import Store


def test_actual_action_history_and_unknown_deletions_survive_upgrade(request,tmp_path,monkeypatch):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply a retained isolated actual-library copy at schema 95')
    source = Path(option).resolve(strict=True)
    target = tmp_path/'action-work.db'
    custody = {}
    copy_readonly_library(source,target,monkeypatch,custody)
    assert custody['source_version']==95, 'Supply a genuine schema-95 historical donor'
    store = Store(target)
    try:
        store._bind_repositories()
        original = {row['id']:dict(row) for row in store.conn.execute('SELECT * FROM actions')}
        assert len(original)>=742
        assert len({row['kind'] for row in original.values()})>=12
        sequence = store.conn.execute("SELECT seq FROM sqlite_sequence WHERE name='actions'").fetchone()
        highwater = sequence[0] if sequence else max(original,default=0)
        protected = ('recordings','catalog_entries','metadata_observations','metadata_conflicts','predictions','prediction_inputs','recommendation_pools')
        def counts():
            return {name:store.conn.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in protected}
        before = counts()
        started = time.monotonic()
        # Populated historical conversion is an explicit test operation. Normal
        # admission must keep legacy owners usable without replaying bulk work.
        migrate(store.conn,registry=registry.MIGRATIONS)
        store.init_schema()
        elapsed = time.monotonic()-started
        assert counts()==before
        assert {row[0] for row in store.conn.execute('SELECT id FROM actions')}==set(original)
        assert store.conn.execute("SELECT seq FROM sqlite_sequence WHERE name='actions'").fetchone()[0]>=highwater
        for row in store.conn.execute('SELECT * FROM actions'):
            old = original[row['id']]
            assert (row['kind'],row['status'],row['created_at'],row['executed_at'])==(
                old['kind'],old['status'],old['created_at'],old['executed_at'])
            capture = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],kind='action',owner=row['id'])
            assert capture=={'params':old['params_json'],'plan':old['plan_json'],'undo':old['undo_json']}
        pending = store.conn.execute("SELECT s.*,t.account_id,t.provider_playlist_id_snapshot FROM action_steps s "
            "JOIN action_targets t ON t.id=s.target_id JOIN actions a ON a.id=s.action_id WHERE a.kind='gc_generated' AND a.status='delete_pending'").fetchall()
        assert len(pending)>=19
        assert all(row['phase']=='uncertain' and row['outcome']=='unknown' for row in pending)
        for row in pending:
            assert store.actions.get_pending_deletion(row['account_id'],row['provider_playlist_id_snapshot']).id==row['action_id']
        drafts = store.conn.execute('SELECT * FROM action_drafts').fetchall()
        assert drafts
        for draft in drafts:
            assert store.actions.get_draft(draft['signature']).plan_json==original[draft['id']]['plan_json']
        for row in action_backup_files.inventory(store.conn):
            if row['availability']=='verified':
                action_backup_files.verified(row['retained_filename'],row['digest'],row['bytes'])
        current.validate(store.conn)
        store.conn.close()
        store = Store(target)
        store.init_schema()
        assert counts()==before and len(store.actions.get_actions())==len(original)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        print({'actions':len(original),'uncertain_gc_deletions':len(pending),'drafts':len(drafts),
               'backup_availability':dict(store.conn.execute('SELECT availability,count(*) FROM action_backups GROUP BY availability')),
               'protected':before,'upgrade_seconds':elapsed,'sqlite':sqlite3.sqlite_version})
    finally:
        store.conn.close()
