"""Explicit actual captured-input custody check, using a read-only donor.

This isolates the sample owner and original content. Public recovery tests
separately cover complete SQL graphs and supported Store startup.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock
from types import SimpleNamespace

from yt_playlist.core import sample_files
from yt_playlist.repos.audio_vibes import AudioVibesRepo


def test_actual_captured_inputs_have_independent_exact_custody(tmp_path):
    donor = Path('.pytest_cache/621-takeover/tuneconsole-startup.db').resolve(strict=True)
    with closing(sqlite3.connect(donor.as_uri()+'?mode=ro', uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute('BEGIN')
        rows = list(source.execute(sample_files.REFERENCES))
        assert len(rows) == 715
        schema = source.execute("SELECT sql FROM sqlite_schema WHERE name='sample_attributes'").fetchone()[0]
        original = {row['id']: Path(row['filename']).read_bytes() for row in rows}
        expected = {row['id']: sample_files.read(row['filename'], expected={key: row[key]
                    for key in ('id', 'run_id', 'point', 'video_id')}) for row in rows}
        manifest = sample_files.export(source, tmp_path/'sample-backup')
    # Keep actual IDs and every owner column. This domain-only snapshot does
    # not claim to audit the surrounding measurement graph.
    target_path = tmp_path/'independent'/'samples.db'
    target_path.parent.mkdir()
    with closing(sqlite3.connect(target_path)) as target:
        target.row_factory = sqlite3.Row
        target.execute(schema)
        target.executemany('INSERT INTO sample_attributes('
            'id,recording_id,video_id,run_id,point,captured_at,filename) VALUES (?,?,?,?,?,?,?)',
            [tuple(row) for row in rows])
        target.commit()
        sample_files.restore(target, manifest)
        identities = {row[0]: list(row[:-1]) for row in rows}
        for row in target.execute(sample_files.REFERENCES):
            assert list(row[:-1]) == identities[row[0]]
            assert Path(row[-1]).is_relative_to(sample_files.directory(target))
            assert Path(row[-1]).read_bytes() == original[row[0]]
        repo = AudioVibesRepo(SimpleNamespace(conn=target, _lock=RLock(), _schema_compatibility=False))
        observed = {sample['id']: sample for run in {row['run_id'] for row in rows}
                    for sample in repo.samples(run)}
        assert observed == expected
        (tmp_path/'actual-sample-custody.json').write_text(json.dumps(dict(
            donor=str(donor), samples=len(rows), exact_bytes=True, normal_reader_exact=True,
            scope='Actual sample owner only; surrounding measurement graph covered separately',
            source_hashes={key: hashlib.sha256(value).hexdigest() for key,value in original.items()}),
            indent=2)+'\n')
