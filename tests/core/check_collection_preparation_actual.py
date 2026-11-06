"""Explicit sync/control preparation on a supplied disposable actual library.

Never collected by pytest. Keeps installed settings and numbered history exact;
no runtime workers, provider clients or native database are used.
"""
from dataclasses import asdict
import faulthandler
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import album_tuning_state, collection_recovery, model_selection_heads, model_state
from yt_playlist.core.preparation import (artist_lookup, checkpoint_publication, collection_controls, control_publication,
    collection_documents, library_sync, model_artifacts, model_checkpoints, model_publication, model_trials, ranked_state, saved_outputs, taste_controls, tv_state, workflow_receipts)
from yt_playlist.core.store import Store


def fingerprint(rows):
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        count += 1
    return {'count': count, 'sha256': digest.hexdigest()}


def check(path, report_path, *, publish_controls=False, checkpoints=False, encoders=False,
          publish_checkpoints=False, publish_encoders=False, trials=False, saved=False, tv=False, ranked=False, taste=False, documents=False, workflows=False):
    assert path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'snapshot': str(path), 'bytes': path.stat().st_size, 'status': 'failed',
              'python': sys.version, 'sqlite': sqlite3.sqlite_version, 'jobs': {},
              'controls': 'Real explicit preparation; no runtime, provider or network workers.'}
    conn = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=.1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        sources = fingerprint(conn.execute('SELECT key,value FROM settings ORDER BY key'))
        document_sources = {owner: fingerprint(conn.execute(f'SELECT {key},{field},created_at FROM main.{owner} ORDER BY {key}'))
            for name, (owner, key, field) in collection_documents.SOURCES.items() if name != 'import'} if documents else {}
        jobs = {'library-sync': library_sync.RESULT_JOB, 'sync-stamps': library_sync.STAMP_JOB,
                **collection_controls.JOBS}
        if checkpoints:
            jobs.update(model_checkpoints.JOBS)
        if encoders:
            jobs.update({'content-encoders': model_artifacts.ARTIFACT_JOB,
                         'encoder-fingerprints': model_artifacts.FINGERPRINT_JOB})
        if trials:
            jobs.update({'trial-' + name: job for name, job in model_trials.JOBS.items()})
        if saved:
            jobs.update({'saved-' + name: job for name, job in saved_outputs.JOBS.items()})
        if tv:
            jobs.update({'tv-aliases': artist_lookup.JOB, **{'tv-' + name: job for name, job in tv_state.JOBS.items()}})
        if ranked:
            jobs.update({'ranked-' + name: job for name, job in ranked_state.JOBS.items()})
            jobs['ranked-roots'] = ranked_state.ROOT_JOB
        if taste:
            jobs.update({'taste-' + name: job for name, job in taste_controls.JOBS.items()})
        if documents:
            jobs.update({'documents-' + name: job for name, job in collection_documents.JOBS.items()})
        if workflows:
            jobs.update({'workflow-' + name: job for name, job in workflow_receipts.JOBS.items()})
        for name, job in jobs.items():
            started = time.monotonic()
            job.install(conn)
            detail = report['jobs'][name] = {'installation_seconds': time.monotonic() - started, 'batches': []}
            reservations = []
            def trace(sql):
                if sql == 'BEGIN IMMEDIATE':
                    reservations.append(time.monotonic())
            conn.set_trace_callback(trace)
            detail['writer_batches'] = []
            for _ in range(1000 if name == 'tv-aliases' or name.startswith('documents-') else 300):
                reservations.clear()
                started = time.monotonic()
                progress = job.step(conn, batch_size=32 if name == 'tv-aliases' else 16)
                finished = time.monotonic()
                detail['batches'].append(finished - started)
                detail['writer_batches'].append(finished - reservations[-1] if reservations else 0)
                if progress.status == 'prepared':
                    break
            conn.set_trace_callback(None)
            assert progress.status == 'prepared'
            detail['progress'] = asdict(progress)
            report_path.write_text(json.dumps(report, indent=2))
        row = conn.execute(f'SELECT g.* FROM {library_sync.CURRENT} h '
            f'JOIN {library_sync.HEAD} g USING(source,generation)').fetchone()
        installed = conn.execute("SELECT value FROM settings WHERE key='library_sync_state'").fetchone()
        assert (row is not None) == (installed is not None)
        assert (library_sync.read_original(conn, row) if row else None) == (installed[0] if installed else None)
        report['jobs']['library-sync']['source_count'] = int(installed is not None)
        report['jobs']['library-sync']['cursor_count'] = row['item_count'] if row else 0
        report['jobs']['sync-stamps']['source_count'] = conn.execute(
            f'SELECT count(*) FROM {library_sync.STAMPS}').fetchone()[0]
        for name, job in collection_controls.JOBS.items():
            expected = dict(conn.execute('SELECT key,value FROM settings WHERE ' + job._filter('settings')))
            table = collection_controls.ALBUM if name == 'album-controls' else collection_controls.RUNS
            archive = collection_controls.originals(job.name)
            actual = dict(conn.execute(f'SELECT p.source,o.raw_value FROM {table} p JOIN {archive} o '
                                      'ON o.source=p.source AND o.digest=p.source_digest'))
            assert actual == expected
            report['jobs'][name]['source_count'] = len(expected)
        if checkpoints:
            for name, job in model_checkpoints.JOBS.items():
                expected = dict(conn.execute('SELECT key,value FROM settings WHERE ' + job._filter('settings')))
                actual = {}
                for key in model_checkpoints.SOURCES[job.name]:
                    row = conn.execute('SELECT * FROM ' + model_checkpoints.TABLES[key]).fetchone()
                    if row is None:
                        assert key not in expected
                        continue
                    raw = conn.execute('SELECT raw_value FROM ' + collection_controls.originals(job.name) +
                        ' WHERE source=? AND digest=?', (key, row['source_digest'])).fetchone()[0]
                    actual[key] = raw
                    kind, values = model_checkpoints.values(conn, key, raw)
                    assert row['source_kind'] == kind
                    assert {field: row[field] for field in values} == values
                assert actual == expected
                report['jobs'][name]['source_count'] = len(expected)
        if encoders:
            expected = dict(conn.execute('SELECT key,value FROM settings WHERE ' + model_artifacts.DEFINITION._filter('settings')))
            rows = conn.execute(f'SELECT g.* FROM {model_artifacts.CURRENT} h JOIN {model_artifacts.HEAD} g '
                                'USING(source,generation)').fetchall()
            assert {row['source']: model_artifacts.read_original(conn, row) for row in rows} == expected
            report['jobs']['content-encoders']['source_count'] = len(expected)
            report['jobs']['content-encoders']['original_bytes'] = sum(row['raw_size'] for row in rows)
            report['jobs']['content-encoders']['parts'] = sum(row['part_count'] for row in rows)
            expected = dict(conn.execute('SELECT key,value FROM settings WHERE ' + model_artifacts.FINGERPRINT_JOB._filter('settings')))
            actual = dict(conn.execute(f'SELECT p.source,o.raw_value FROM {model_artifacts.FINGERPRINT_TABLE} p '
                f'JOIN {model_artifacts.ORIGINALS} o ON o.source=p.source AND o.digest=p.source_digest'))
            assert actual == expected
            report['jobs']['encoder-fingerprints']['source_count'] = len(expected)
        if publish_checkpoints:
            report['checkpoint_publications'] = {}
            for name, job in model_checkpoints.JOBS.items():
                started = time.monotonic()
                revision = job.progress(conn).prepared_revision
                checkpoint_publication.publish(conn, name, revision=revision)
                assert checkpoint_publication.selected(conn, name) == revision
                expected = dict(conn.execute('SELECT key,value FROM settings WHERE ' + job._filter('settings')))
                actual = dict(conn.execute(f'SELECT source,raw_value FROM {checkpoint_publication.ORIGINALS} '
                                          'WHERE name=? AND revision=?', (name, revision)))
                assert actual == expected
                report['checkpoint_publications'][name] = {'revision': revision,
                    'seconds': time.monotonic() - started, 'originals_exact': True, 'source_count': len(expected)}
            runtime = Store(path)
            try:
                runtime.init_schema(defer_metadata=True)
                served_checkpoints = {name: {key: runtime.settings.get_setting(key, 'missing')
                    for key in model_checkpoints.SOURCES[job.name]}
                    for name, job in model_checkpoints.JOBS.items()}
            finally:
                runtime.conn.close()
            reopened = Store(path)
            try:
                reopened.init_schema(defer_metadata=True)
                for name, job in model_checkpoints.JOBS.items():
                    revision = report['checkpoint_publications'][name]['revision']
                    assert checkpoint_publication.selected(reopened.conn, name) == revision
                    assert {key: reopened.settings.get_setting(key, 'missing')
                            for key in model_checkpoints.SOURCES[job.name]} == served_checkpoints[name]
                report['checkpoint_reopened_runtime'] = 'passed'
            finally:
                reopened.conn.close()
        if trials:
            for name, surface in model_trials.SURFACES.items():
                generations, current, originals = model_trials.tables(surface)
                expected = conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
                row = conn.execute(f'SELECT o.raw_value,o.built_at FROM {current} h JOIN {generations} g '
                    f'USING(source,preparation_revision) JOIN {originals} o ON o.source=g.source AND o.digest=g.source_digest').fetchone()
                assert (tuple(row) if row else None) == (tuple(expected) if expected else None)
                report['jobs']['trial-' + name]['source_count'] = int(expected is not None)
            report['model_trial_baseline_items'] = conn.execute(f'SELECT count(*) FROM {model_trials.BASELINE}').fetchone()[0]
        if saved:
            for name, source in saved_outputs.SOURCES.items():
                generations, current = saved_outputs.tables(source)
                expected = conn.execute('SELECT value FROM settings WHERE key=?', (source,)).fetchone()
                row = conn.execute(f'SELECT g.* FROM {current} h JOIN {generations} g USING(source,preparation_revision)').fetchone()
                assert (row is not None) == (expected is not None)
                if row is not None:
                    assert saved_outputs.read_original(row) == expected[0]
                detail = report['jobs']['saved-' + name]
                detail.update(source_count=int(expected is not None), original_bytes=row['raw_size'] if row else 0,
                              source_kind=row['source_kind'] if row else 'absent')
                if row is not None and name == 'radio-seed':
                    subjects = conn.execute(f'SELECT position,video_id FROM {saved_outputs.SUBJECTS} '
                        'WHERE preparation_revision=? ORDER BY position', (row['preparation_revision'],)).fetchall()
                    tracks = (json.loads(expected[0]).get('tracks', []) if expected[0] else [])
                    assert list(map(tuple, subjects)) == list(enumerate(track['video_id'] for track in tracks))
                    detail.update(subject_count=len(subjects), policy_count=row['policy_count'],
                        missing_entries=conn.execute(f'SELECT count(*) FROM {saved_outputs.SUBJECTS} '
                            'WHERE preparation_revision=? AND resolution_reason IS NOT NULL', (row['preparation_revision'],)).fetchone()[0])
        if tv:
            report['jobs']['tv-aliases']['source_count'] = conn.execute('SELECT count(*) FROM artist_aliases').fetchone()[0]
            assert report['jobs']['tv-aliases']['source_count'] == conn.execute('SELECT count(*) FROM ' + artist_lookup.ALIASES).fetchone()[0]
            for name, surface in tv_state.SOURCES.items():
                generations, current, _parts = tv_state.tables(surface)
                expected = conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
                row = conn.execute(f'SELECT g.* FROM {current} h JOIN {generations} g USING(source,preparation_revision)').fetchone()
                assert (row is not None) == (expected is not None)
                detail = report['jobs']['tv-' + name]
                detail['source_count'] = int(expected is not None)
                if row is not None:
                    assert (tv_state.read_original(conn, row), row['built_at']) == tuple(expected)
                    actual = sum(conn.execute('SELECT count(*) FROM ' + tv_state.relation(table) +
                        ' WHERE preparation_revision=?', (row['preparation_revision'],)).fetchone()[0] for table in tv_state.RELATIONS[surface])
                    assert actual == row['row_count']
                    detail.update(original_bytes=row['raw_size'], parts=row['part_count'], typed_rows=actual)
        if ranked:
            for name, job in ranked_state.JOBS.items():
                expected = list(conn.execute('SELECT surface,payload,built_at FROM rec_proposals WHERE ' + job.definition._filter('rec_proposals')))
                actual = list(conn.execute(f'SELECT surface FROM prepared_{"model_pairs" if name == "pairs" else "clicked_pools"}_heads'))
                assert {row[0] for row in actual} == {row[0] for row in expected}
                for surface, raw, at in expected:
                    row = ranked_state.head(conn, surface)
                    assert (ranked_state.read_original(conn, row), row['built_at']) == (raw, at)
                report['jobs']['ranked-' + name]['source_count'] = len(expected)
                report['jobs']['ranked-' + name]['source_fingerprint'] = fingerprint(expected)
            root_count = 0
            for owner in ranked_state.home_preparation.ROOT_KEYS:
                expected = list(conn.execute('SELECT * FROM ' + owner + ' WHERE ' + ranked_state.ROOT_JOB._filter(owner)))
                for row in expected:
                    key = json.dumps([row[field] for field in ranked_state.home_preparation.ROOT_KEYS[owner]], separators=(',', ':'), ensure_ascii=False)
                    archived = conn.execute(f'SELECT o.source_snapshot FROM {ranked_state.ROOTS} p JOIN {ranked_state.ORIGINALS} o '
                        'ON o.owner=p.owner AND o.source_key=p.source_key AND o.digest=p.source_digest WHERE p.owner=? AND p.source_key=?',
                        (owner, key)).fetchone()
                    assert json.loads(archived[0]) == dict(row)
                root_count += len(expected)
            assert root_count == conn.execute('SELECT count(*) FROM ' + ranked_state.ROOTS).fetchone()[0]
            report['jobs']['ranked-roots']['source_count'] = root_count
        if taste:
            for name, source in taste_controls.SOURCES.items():
                expected = conn.execute('SELECT value FROM settings WHERE key=?', (source,)).fetchone()
                row = taste_controls.head(conn, source)
                assert (row is not None) == (expected is not None)
                if row is not None:
                    assert taste_controls.read_original(conn, row) == expected[0]
                    value = json.loads(expected[0]) if expected[0] else None
                    if name == 'checkpoint' and value is not None:
                        assert row['saved_at'] == value['saved_at']
                        assert collection_recovery.read(row['recovery_filename'], row['recovery_digest'], row['recovery_bytes'],
                            kind='taste-checkpoint', owner=1) == value['params']
                        weights = conn.execute('SELECT position,captured_axis,weight,updated_at FROM prepared_taste_checkpoint_weights '
                            'WHERE source=? AND preparation_revision=? ORDER BY position', (source, row['preparation_revision'])).fetchall()
                        assert list(map(tuple, weights)) == [(index, weight['axis'], weight['weight'], weight['updated_at'])
                            for index, weight in enumerate(value['weights'])]
                        report['jobs']['taste-' + name]['typed_weights_exact'] = True
                report['jobs']['taste-' + name].update(source_count=int(expected is not None),
                    original_bytes=row['raw_size'] if row else 0, member_count=row['member_count'] if row else 0)
        if documents:
            for name, (owner, key, field) in collection_documents.SOURCES.items():
                job = collection_documents.JOBS[name]
                rows = conn.execute(f'SELECT {key},{field},' + ('NULL' if name == 'import' else 'created_at') + f' FROM main.{owner}' +
                    (' WHERE ' + job.definition._filter(owner) if name == 'import' else '') + f' ORDER BY {key}').fetchall()
                assert {row[0] for row in rows} == {row[0] for row in conn.execute('SELECT source FROM ' + collection_documents.table(name, 'heads'))}
                byte_count, facet_count = 0, 0
                for source, raw, at in rows:
                    captured = collection_documents.head(conn, name, source)
                    assert collection_documents.read_original(conn, name, captured) == raw
                    assert captured['created_at'] == at
                    byte_count += captured['raw_size']
                    if name == 'recipes':
                        value = json.loads(raw)
                        selections = value.get('facets') or {}
                        actual = conn.execute('SELECT kind,position,captured_value FROM prepared_playlist_documents_facets '
                            'WHERE source=? AND preparation_revision=? ORDER BY kind,position', (source, captured['preparation_revision'])).fetchall()
                        expected = sorted((kind, index, label) for kind, field in (('genre', 'genres'), ('era', 'eras'))
                            for index, label in enumerate(selections.get(field) or []))
                        assert list(map(tuple, actual)) == expected
                        facet_count += len(actual)
                        kind, recovered = 'playlist-recipe', value
                    elif name == 'canvases':
                        kind, recovered = 'playlist-canvas', raw
                    else:
                        value = json.loads(raw) if raw else None
                        kind, recovered = 'spotify-import', {key: value[key] for key in ('report','preview_token') if key in value} if value else None
                    if captured['state_present']:
                        assert collection_recovery.read(captured['recovery_filename'], captured['recovery_digest'], captured['recovery_bytes'],
                            kind=kind, owner=source) == recovered
                report['jobs']['documents-' + name].update(source_count=len(rows), original_bytes=byte_count,
                    facets=facet_count, originals_exact=True, recovery_exact=True, captured_times_exact=True)
        if workflows:
            for name, job in workflow_receipts.JOBS.items():
                expected = dict(conn.execute('SELECT key,value FROM settings WHERE ' + job.definition._filter('settings')))
                assert set(expected) == {row[0] for row in conn.execute('SELECT source FROM ' + workflow_receipts.table(name, 'heads'))}
                members, slots = 0, 0
                for source, raw in expected.items():
                    captured = workflow_receipts.head(conn, name, source)
                    assert workflow_receipts.read_original(conn, name, captured) == raw
                    value = json.loads(raw) if raw else None
                    suffix = 'attempts' if name == 'gc' else 'receipts'
                    rows = conn.execute('SELECT * FROM ' + workflow_receipts.table(name, suffix) +
                        ' WHERE source=? AND preparation_revision=? ORDER BY 1,2,3', (source, captured['preparation_revision'])).fetchall()
                    assert len(rows) == captured['member_count']
                    members += len(rows)
                    if name == 'gc':
                        for row in rows:
                            assert collection_recovery.read(row['recovery_filename'], row['recovery_digest'], row['recovery_bytes'],
                                kind='playlist-gc', owner=source) == value
                            assert (row['attempts'], row['last_attempt_at'], row['next_retry_at'], row['provider_playlist_id_snapshot']) == (
                                value['attempts'], value['last_attempt_at'], value['next_retry_at'], value['ytm'])
                    else:
                        ordered = list(value.items()) if isinstance(value, dict) else [(item, None) for item in value or []]
                        assert [(row['position'], row['fingerprint']) for row in rows] == list(enumerate(item[0] for item in ordered))
                        for row, (receipt_fingerprint, receipt) in zip(rows, ordered):
                            recovered = None if receipt is None else {key: item for key, item in receipt.items() if key != '_receipt_revision'}
                            owner = json.dumps([source, receipt_fingerprint], ensure_ascii=False, separators=(',', ':'))
                            assert collection_recovery.read(row['recovery_filename'], row['recovery_digest'], row['recovery_bytes'],
                                kind='spotify-transfer', owner=owner) == recovered
                            items = conn.execute('SELECT position,video_id_snapshot FROM ' + workflow_receipts.table(name, 'items') +
                                ' WHERE source=? AND preparation_revision=? AND receipt_position=? ORDER BY position',
                                (source, captured['preparation_revision'], row['position'])).fetchall()
                            assert list(map(tuple, items)) == list(enumerate((receipt or {}).get('source_video_ids', [])))
                            slots += len(items)
                report['jobs']['workflow-' + name].update(source_count=len(expected), members=members, slots=slots, originals_exact=True, recovery_exact=True)
        if publish_controls:
            report['publications'] = {}
            for name, job in collection_controls.JOBS.items():
                started = time.monotonic()
                revision = job.progress(conn).prepared_revision
                control_publication.publish(conn, name, revision=revision)
                assert control_publication.selected(conn, name) == revision
                expected = dict(conn.execute('SELECT key,value FROM settings WHERE ' + job._filter('settings')))
                actual = dict(conn.execute(f'SELECT source,raw_value FROM {control_publication.ORIGINALS} '
                                          'WHERE name=? AND revision=?', (name, revision)))
                assert actual == expected
                report['publications'][name] = {'revision': revision,
                    'seconds': time.monotonic() - started, 'originals_exact': True,
                    'source_count': len(expected)}
            reopened = Store(path)
            try:
                reopened.init_schema(defer_metadata=True)
                for name in collection_controls.JOBS:
                    revision = report['publications'][name]['revision']
                    assert control_publication.selected(reopened.conn, name) == revision
                for key in model_selection_heads.KEYS:
                    row = conn.execute(f'SELECT recorded_run_id FROM {control_publication.RUNS} '
                                       'WHERE revision=? AND source=?',
                                       (report['publications']['model-pointers']['revision'], key)).fetchone()
                    expected = (str(row[0]) if row[0] is not None else '') if row else 'missing'
                    assert reopened.settings.get_setting(key, 'missing') == expected
                assert album_tuning_state.available(reopened.conn)
                report['reopened_runtime'] = 'passed'
            finally:
                reopened.conn.close()
        if publish_encoders:
            revisions = {'artifact_revision': model_artifacts.ARTIFACT_JOB.progress(conn).prepared_revision,
                         'fingerprint_revision': model_artifacts.FINGERPRINT_JOB.progress(conn).prepared_revision}
            reserved = []
            def trace(sql):
                if sql == 'BEGIN IMMEDIATE':
                    reserved.append(time.monotonic())
            started = time.monotonic()
            conn.set_trace_callback(trace)
            try:
                model_publication.publish(conn, **revisions)
                finished = time.monotonic()
            finally:
                conn.set_trace_callback(None)
            assert len(reserved) == 1
            assert model_publication.selected(conn) == tuple(revisions.values())
            expected = dict(conn.execute('SELECT key,value FROM settings WHERE key IN (' +
                                        ','.join(repr(key) for key in model_state.FIELDS) + ')'))
            reopened = Store(path)
            try:
                reopened.init_schema(defer_metadata=True)
                assert model_publication.selected(reopened.conn) == tuple(revisions.values())
                for key in model_state.FIELDS:
                    raw = expected.get(key)
                    value = raw if key in model_artifacts.FINGERPRINTS and raw is not None else raw or 'missing'
                    assert reopened.settings.get_setting(key, 'missing') == value
                report['encoder_publication'] = {**revisions, 'seconds': finished - started,
                    'writer_seconds': finished - reserved[0], 'reopened_runtime': 'passed', 'source_count': len(expected)}
            finally:
                reopened.conn.close()
        assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        assert fingerprint(conn.execute('SELECT key,value FROM settings ORDER BY key')) == sources
        if documents:
            assert document_sources == {owner: fingerprint(conn.execute(f'SELECT {key},{field},created_at FROM main.{owner} ORDER BY {key}'))
                for name, (owner, key, field) in collection_documents.SOURCES.items() if name != 'import'}
            report['document_sources_exact'] = True
        report.update(status='passed', historical_version=history[-1][0], history_exact=True,
                      settings_exact=True, settings=sources)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        report_path.write_text(json.dumps(report, indent=2))
        conn.close()


if __name__ == '__main__':
    faulthandler.dump_traceback_later(90, exit=True)
    try:
        check(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), publish_controls='--publish-controls' in sys.argv[3:],
              checkpoints=bool({'--checkpoints', '--publish-checkpoints'} & set(sys.argv[3:])),
              encoders=bool({'--encoders', '--publish-encoders'} & set(sys.argv[3:])),
              publish_checkpoints='--publish-checkpoints' in sys.argv[3:], publish_encoders='--publish-encoders' in sys.argv[3:],
              trials='--trials' in sys.argv[3:], saved='--saved-outputs' in sys.argv[3:], tv='--tv' in sys.argv[3:], ranked='--ranked' in sys.argv[3:],
              taste='--taste-controls' in sys.argv[3:], documents='--collection-documents' in sys.argv[3:], workflows='--workflow-receipts' in sys.argv[3:])
    finally:
        faulthandler.cancel_dump_traceback_later()
