"""Canonical genre upgrade against an offline populated library, never live state."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import pytest

from yt_playlist.core import genre_classifications, genre_identity
from yt_playlist.core.migrations import current, migrate


def test_populated_genre_upgrade(request, tmp_path):
    option = request.config.getoption('--schema-source-db')
    if not option:
        pytest.skip('Supply an offline --schema-source-db predecessor')
    path = Path(option).resolve(strict=True)
    assert not Path(str(path)+'-wal').exists(), 'Use a checkpointed offline donor'
    def fingerprint():
        with path.open('rb') as source:
            return hashlib.file_digest(source,'sha256').hexdigest()
    original_hash = fingerprint()
    source = sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)
    source.row_factory = sqlite3.Row
    target = sqlite3.connect(tmp_path/'genres.db')
    target.row_factory = sqlite3.Row
    source.execute('PRAGMA query_only=ON')
    source.execute('BEGIN')
    try:
        version = source.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
        assert version in (84,85), 'Use the populated label-based predecessor'
        upgraded = request.config.getoption('--schema-upgraded-db')
        if upgraded:
            donor_path = Path(upgraded).resolve(strict=True)
            assert not Path(str(donor_path)+'-wal').exists(), 'Checkpoint the isolated recovery donor first'
            donor = sqlite3.connect(donor_path.as_uri()+'?mode=ro&immutable=1',uri=True)
            try:
                donor.backup(target)
            finally:
                donor.close()
        else:
            source.backup(target)
        target.execute('PRAGMA foreign_keys=ON')
        selected = {table:[tuple(row) for row in source.execute(f'SELECT {column},genre FROM {table}')]
                    for table,column in genre_classifications.OWNERS.items()}
        expected = {table:{subject:genre_identity.resolve(source,label)
                          for subject,label in rows} for table,rows in selected.items()}
        def contents(conn,table):
            digest,count = hashlib.sha256(),0
            for row in conn.execute('SELECT * FROM '+table):
                digest.update(repr(tuple(row)).encode())
                count += 1
            return count,digest.hexdigest()
        immutable = {table:contents(source,table) for table in ('predictions','prediction_inputs','metadata_observations')}
        enabled = (dict(source.execute('SELECT genre_key,enabled FROM genre_preferences')) if version==85 else None)
        start = time.monotonic()
        print('Copied isolated library; starting canonical genre migration',flush=True)
        migrate(target)
        elapsed = time.monotonic()-start
        unresolved = {}
        for table,column in genre_classifications.OWNERS.items():
            original = dict(selected[table])
            rows = list(target.execute(f'SELECT {column},genre_key,unresolved_genre FROM {table}'))
            assert len(rows)==len(original)
            unresolved[table] = {}
            for subject,key,raw in rows:
                assert key==expected[table][subject][0]
                assert raw==(original[subject] if key is None else None)
                if raw:
                    unresolved[table][raw] = unresolved[table].get(raw,0)+1
        for table,digest in immutable.items():
            assert contents(target,table)==digest
        if enabled is not None:
            assert dict(target.execute('SELECT genre_key,enabled FROM genre_preferences'))==enabled
        before = sum(row[0] for row in source.execute('SELECT score FROM rec_theme'))
        after = sum(row[0] for row in target.execute('SELECT score FROM rec_theme'))
        assert after==pytest.approx(before)
        assert target.execute("SELECT count(*) FROM taste_axis_legacy WHERE table_name='rec_theme'").fetchone()[0]==source.execute('SELECT count(*) FROM rec_theme').fetchone()[0]
        current.validate(target)
        migrate(target)
        assert not target.execute('PRAGMA foreign_key_check').fetchone()
        report = {'donor':str(path),'source_version':version,'target_version':current.VERSION,
                  'source_sha256':original_hash,'seconds':elapsed,'owner_counts':{t:len(rows) for t,rows in selected.items()},
                  'resumed_from':upgraded,'unresolved':unresolved,'immutable_counts':{t:v[0] for t,v in immutable.items()},
                  'preferences':target.execute('SELECT count(*) FROM genre_preferences').fetchone()[0]}
        (tmp_path/'genre-upgrade.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
    finally:
        target.close()
        source.close()
        assert fingerprint()==original_hash
