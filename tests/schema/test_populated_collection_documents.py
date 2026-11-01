"""Opt-in isolated upgrade of actual saved recipes and retained taste settings."""
import json
from pathlib import Path
import sqlite3
import time

import pytest

from tests.schema.actual_library import copy_readonly_library
from yt_playlist.core import collection_recovery, genre_identity, taste_control_state
from yt_playlist.core.migrations import current, migrate, registry
from yt_playlist.core.store import Store


def test_actual_collection_documents_upgrade_and_restart(request,tmp_path,monkeypatch):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply a retained isolated actual-library copy at schema 95')
    source = Path(option).resolve(strict=True)
    target = tmp_path/'collection-documents.db'
    custody = {}
    copy_readonly_library(source,target,monkeypatch,custody)
    assert custody['source_version']==95, 'Supply a genuine schema-95 historical donor'
    store = Store(target)
    try:
        store._bind_repositories()
        recipes = {row[0]:(json.loads(row[1]),row[2]) for row in store.conn.execute('SELECT * FROM rec_recipes')}
        canvases = dict(store.conn.execute('SELECT playlist_ytm,state FROM cluster_canvas'))
        assert len(recipes)>=150 and len(canvases)>=7
        checkpoint = json.loads(store.settings.get_setting(taste_control_state.CHECKPOINT))
        assert len(checkpoint['weights'])>=150
        expected = {}
        for scope,(original,_) in recipes.items():
            value = json.loads(json.dumps(original))
            if value.get('facets',{}).get('genres') is not None:
                labels = []
                for label in value['facets']['genres']:
                    family = store.conn.execute('SELECT key FROM genre_family WHERE key=?',(label,)).fetchone()
                    key,_ = genre_identity.resolve(store.conn,label)
                    labels.append(family[0] if family else key if key is not None else label)
                value['facets']['genres'] = labels
            expected[scope] = value
        protected = ('recordings','catalog_entries','actions','metadata_observations','metadata_conflicts',
                     'predictions','prediction_inputs','recommendation_pools','album_feedback')
        def counts():
            return {table:store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in protected}
        before = counts()
        started = time.monotonic()
        # Exercise the frozen upgrade route only on this disposable copy.
        migrate(store.conn,registry=registry.MIGRATIONS)
        store.init_schema()
        elapsed = time.monotonic()-started
        assert counts()==before
        assert {scope:store.playlist_recipes.get_recipe(scope) for scope in expected}==expected
        assert {scope:store.playlist_recipes.get_cluster_canvas(scope) for scope in canvases}==canvases
        assert json.loads(store.settings.get_setting(taste_control_state.CHECKPOINT))==checkpoint
        assert store.playlist_recipes.get_recipe_created_ats()=={scope:item[1] for scope,item in recipes.items()}
        for row in store.conn.execute('SELECT * FROM rec_recipes'):
            assert collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],
                                            kind='playlist-recipe',owner=row['id'])==recipes[row['playlist_scope_id']][0]
        current.validate(store.conn)
        restarted = Store(target)
        try:
            restarted.init_schema()
            assert {scope:restarted.playlist_recipes.get_recipe(scope) for scope in expected}==expected
            assert {scope:restarted.playlist_recipes.get_cluster_canvas(scope) for scope in canvases}==canvases
            assert not restarted.conn.execute('PRAGMA foreign_key_check').fetchall()
        finally:
            restarted.conn.close()
        print({'source_schema':95,'target_schema':current.VERSION,'recipes':len(recipes),'canvases':len(canvases),
               'checkpoint_weights':len(checkpoint['weights']),'protected':before,'upgrade_seconds':elapsed,
               'sqlite':sqlite3.sqlite_version})
    finally:
        store.conn.close()
