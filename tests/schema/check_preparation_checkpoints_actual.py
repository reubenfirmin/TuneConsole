"""Check preparation cutover and an interrupted run on a private C175 library."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.library_image.fixtures import managed_database, report_file
from yt_playlist.controllers import group_preparation as grouping
from yt_playlist.core import ranking_checkpoints, recording_selection
from yt_playlist.core.activity import WorkClosing
from yt_playlist.core.content_checkpoint_contract import CONFIG, FIELDS, KEYS, MODES, PHASES
from yt_playlist.core.content_preparation_state import ACKNOWLEDGED, CONTENT_REVISION, EVIDENCE_REVISION, REFRESH_REVISION
from yt_playlist.core.group_preparation_state import ROOTS
from yt_playlist.core.store import Store
from yt_playlist.core.worker_settings import number
from yt_playlist.rec import group_preparation


def _tables(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _before(conn):
    conn.row_factory = sqlite3.Row
    content = conn.execute('SELECT * FROM content_full_checkpoint').fetchone()
    saved = {}
    for key, phase in KEYS.items():
        if not content['has_checkpoint' if phase == 'full' else 'has_' + phase]:
            continue
        columns = {name: name if phase == 'full' else f'{phase}_{name}' for name in FIELDS[key]}
        expected = {name: content[column] for name, column in columns.items() if content[column] is not None}
        if phase == 'full':
            expected['at'] = content['at']
            if content['has_config']:
                expected['config'] = [content[name] for name in CONFIG]
        elif content[f'{phase}_has_phases']:
            expected['phase_bytes'] = {name: content[f'{phase}_bytes_{name}'] for name in PHASES
                                       if content[f'{phase}_bytes_{name}'] is not None}
        if phase == 'basis' and content['basis_has_modes']:
            expected['modes'] = [content['basis_' + name] for name in MODES]
        saved[key] = expected
    group = conn.execute('''SELECT s.*,coalesce(g.external_key,s.unresolved_generation) AS generation
        FROM group_preparation_state s LEFT JOIN group_generations g ON g.id=s.generation_id''').fetchone()
    checkpoint = {'generation': group['generation'], 'serial': group['input_serial'], 'at': group['checkpoint_at']}
    assert checkpoint['generation'], 'The managed library must have a previously completed group build'
    progress = {'phase': group['phase'], 'at': group['reported_at'], 'reason': group['reason']}
    counts = {CONTENT_REVISION: content['content_changes'], EVIDENCE_REVISION: content['evidence_changes'],
              REFRESH_REVISION: content['refresh_revision'], ACKNOWLEDGED: content['acknowledged_refresh_revision']}
    return content, group, saved, checkpoint, progress, counts


def test_preparation_upgrade_and_restart(monkeypatch):
    report = {}
    path = managed_database(report=report)
    if path is None:
        pytest.skip('Requires a private managed C175 snapshot')
    now = report['managed_library']['created_at']
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == 175
        before = _tables(conn)
        content, group, saved, checkpoint, progress, counts = _before(conn)
        # C176 copies retirement receipts onto the corresponding mode rows,
        # each of which advances the original grouping input clock.
        retired_modes = conn.execute('SELECT count(*) FROM taste_mode_retired_payloads').fetchone()[0]

    store = Store(path)
    with closing(store.conn) as conn:
        store.init_schema()
        after = _tables(conn)
        assert {'content_full_checkpoint', 'group_preparation_state'} <= before - after
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        for key, expected in saved.items():
            assert json.loads(store.settings.get_setting(key)) == expected
        for key, column in ranking_checkpoints.COLUMNS.items():
            assert store.settings.get_setting(key) == (None if content[column] is None else str(content[column]))
        assert store.rec_refresh.version() == content['fit_version']
        assert recording_selection.ready(conn) == bool(content['recording_selection_ready'])
        assert {key: number(conn, key) for key in counts} == counts
        assert store.taste_groups.serial() == group['serial'] + retired_modes
        assert store.settings.get_setting('rec_mode_fit_inputs') == group['fit_fingerprint']
        assert number(conn, ROOTS) == group['roots_complete']
        assert json.loads(store.settings.get_setting(grouping.CHECKPOINT)) == checkpoint
        assert group_preparation.status(store) == progress
        published_groups = store.taste_groups.head()
        published_recommendations = store.recommendations.head('home')
        assert published_groups and published_recommendations

        worker = group_preparation.Preparation(store)
        assert worker.due(now)
        with monkeypatch.context() as patch:
            def stop():
                raise WorkClosing('Interrupted during the grouping input read')
            patch.setattr(grouping, 'check_stopping', stop)
            with pytest.raises(WorkClosing):
                worker.step(now)
        assert group_preparation.status(store) == {'phase': 'preparing', 'at': now, 'reason': None}
        assert store.taste_groups.head() == published_groups
        assert store.recommendations.head('home') == published_recommendations
        captured = store.rec_refresh.revision()
        assert captured == counts[REFRESH_REVISION] and captured != counts[ACKNOWLEDGED]
        with conn:
            conn.execute('UPDATE recordings SET bpm=CASE WHEN bpm=101 THEN 102 ELSE 101 END WHERE id=('
                         'SELECT recording_id FROM prepared_recording_inputs WHERE library_present=1 '
                         'AND recording_id IS NOT NULL LIMIT 1)')
        pending = store.rec_refresh.revision()
        assert pending > captured
        store.rec_refresh.acknowledge(captured)
        assert store.rec_refresh.revision() == pending

    store = Store(path)
    with closing(store.conn) as conn:
        store.init_schema()
        assert group_preparation.status(store)['phase'] == 'preparing'
        assert store.taste_groups.head() == published_groups
        assert store.recommendations.head('home') == published_recommendations
        assert store.rec_refresh.revision() == pending
        retry = group_preparation.Preparation(store)
        assert retry.due(now + 60)
        retry.step(now + 60)
        status = group_preparation.status(store)
        assert status['phase'] == 'ready', status
        new_checkpoint = json.loads(store.settings.get_setting(grouping.CHECKPOINT))
        assert new_checkpoint['generation'] == store.taste_groups.head_generation()
        assert new_checkpoint['serial'] == store.taste_groups.serial()
        assert store.recommendations.head('home') == published_recommendations
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()

    report.update(tables_before=len(before), tables_after=len(after), removed=sorted(before - after),
                  compared_content_checkpoints=sorted(saved), initial_group_serial=group['serial'],
                  pending_refresh_before_edit=captured, pending_refresh_after_edit=pending,
                  group_restarted=True, last_publications_readable=True, foreign_key_errors=0)
    report_file('preparation-checkpoints-c178.json').write_text(json.dumps(report, indent=2) + '\n')
