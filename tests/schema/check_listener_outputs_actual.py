"""Check listener and model output cutover on a private populated C175 library."""
from contextlib import closing
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.library_image.fixtures import managed_database, report_file
from yt_playlist.core import collection_recovery, listener_state, model_state, recommendation_outputs
from yt_playlist.core.migrations import control_settings_storage, listener_output_storage
from yt_playlist.core.store import Store
from yt_playlist.rec import autotune_run, taste_analysis
from yt_playlist.web.app import create_app


def _tables(conn):
    return {name for name, in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def test_listener_outputs_upgrade(monkeypatch):
    report = {}
    path = managed_database(report=report)
    if path is None:
        pytest.skip('Requires a private managed C175 snapshot')
    now = report['managed_library']['created_at']
    seeded = []
    with closing(sqlite3.connect(path)) as conn, conn:
        assert conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == 175
        before = _tables(conn)
        if conn.execute('SELECT 1 FROM library_taste_summary').fetchone() is None:
            payload = {'breadth': {'families': {}, 'breadth': 0, 'n_families': 0, 'n_tagged': 0},
                       'eras': [], 'genre_plays': {}, 'genre_options': []}
            conn.execute('INSERT INTO library_taste_summary VALUES (1,?,?)', (json.dumps(payload), now))
            seeded.append('library_taste_summary')
        summary, summary_at = conn.execute('SELECT payload,built_at FROM library_taste_summary').fetchone()
        for surface in listener_state.SURFACES:
            if conn.execute('SELECT 1 FROM listener_model_documents WHERE surface=?', (surface,)).fetchone() is None:
                conn.execute('INSERT INTO listener_model_documents VALUES (?,?,?)',
                             (surface, json.dumps({'weights': {}, 'version': 'c175-snapshot'}), now))
                seeded.append(surface)
        listener = {key: (raw, at) for key, raw, at in conn.execute('SELECT * FROM listener_model_documents')}
        models, encoders = {}, {}
        for kind in model_state.KEYS:
            fields = ('revision', 'generation', 'input_fingerprint', 'genre_revision', 'fit_fingerprint',
                      *model_state.REFERENCE_FIELDS)
            sql = f'SELECT {",".join(fields)} FROM taste_model_state WHERE id=?'
            row = conn.execute(sql, (kind,)).fetchone()
            if row is None or row[-3] is None:
                reference = collection_recovery.publish(conn, 'taste-settings', kind, json.dumps({'encoder': kind}))
                conn.execute('INSERT INTO taste_model_state VALUES (?,1,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                             'recovery_filename=excluded.recovery_filename,recovery_digest=excluded.recovery_digest,'
                             'recovery_bytes=excluded.recovery_bytes',
                             (kind, 'generation', 'inputs', 'genres', kind + ' fit', *reference))
                row = conn.execute(sql, (kind,)).fetchone()
                seeded.append(kind + '_model_state')
            models[kind] = dict(zip(fields, row, strict=True))
            encoders[kind] = collection_recovery.read(*row[-3:], kind='taste-settings', owner=kind)
        output_fields = ('observed_at', 'filename', 'content_digest', 'byte_size')
        report_sql = f'SELECT {",".join(output_fields)} FROM model_autotune_output'
        row = conn.execute(report_sql).fetchone()
        surface = autotune_run.RESULT_SETTING
        if row is None:
            encoded = recommendation_outputs.encode(surface, '', json.dumps({'ran_at': now}))
            reference = recommendation_outputs.publish(recommendation_outputs.directory(conn), encoded)
            conn.execute('INSERT INTO model_autotune_output(id,observed_at,filename,content_digest,byte_size) '
                         'VALUES (1,?,?,?,?)', (now, *reference))
            row = conn.execute(report_sql).fetchone()
            seeded.append(surface)
        autotune = dict(zip(output_fields, row, strict=True))
        report_text = recommendation_outputs.read(*row[1:], surface=surface, chunk_key='')

    for _ in range(2):
        store = Store(path)
        with closing(store.conn) as conn:
            store.init_schema()
            after = _tables(conn)
            removed = set(control_settings_storage.RETIRED) | set(listener_output_storage.RETIRED)
            assert removed <= before - after
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()
            assert taste_analysis.library_summary(store) == json.loads(summary)
            assert json.loads(store.settings.get_setting('library_taste_summary')) == {
                'payload': summary, 'built_at': summary_at}
            for key, (raw, at) in listener.items():
                assert store.rec_proposals.has_proposals(key)
                assert store.rec_proposals.get_proposals(key) == json.loads(raw)
                assert json.loads(store.settings.get_setting(key)) == {'payload': raw, 'updated_at': at}
            for kind, expected in models.items():
                assert json.loads(store.settings.get_setting(model_state.KEYS[kind])) == expected
                assert getattr(store.rec_model, kind + '_encoder')() == encoders[kind]
            assert recommendation_outputs.autotune_reference(conn) == autotune
            assert store.settings.get_setting(surface) == report_text

    store = Store(path)
    with closing(store.conn):
        store.init_schema()
        account_id = store.accounts.get_master_account().id
        monkeypatch.setattr('yt_playlist.core.updatecheck._raw_version', lambda: '0.0.0.dev0')
        app = create_app(store, lambda: {account_id: FakeClient()}, now_fn=lambda: now, background_work=False)
        with TestClient(app, base_url='http://127.0.0.1') as client:
            for url in ('/', '/taste/autotune-status'):
                response = client.get(url)
                assert response.status_code == 200, (url, response.text[:500])
    report.update(tables_before=len(before), tables_after=len(after), removed=sorted(before-after),
                  seeded_missing_outputs=seeded, preserved_listener_outputs=len(listener),
                  preserved_encoder_references=len(encoders), reopened=True, foreign_key_errors=0)
    report_file('listener-outputs-c177.json').write_text(json.dumps(report, indent=2) + '\n')
