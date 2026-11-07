"""Keep persisted readiness hashes compatible while batching cursor reads."""
from contextlib import closing
import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.repos.musical_profiles import MusicalProfilesRepo
from yt_playlist.controllers.musical_profiles import MusicalProfiles


@pytest.mark.parametrize('normalized', [False, True])
@pytest.mark.parametrize('count', [0, 3, 515])
def test_readiness_hash_retains_exact_legacy_page_bytes(tmp_path, normalized, count):
    with closing(sqlite3.connect(tmp_path / 'fingerprint.db', factory=DiagnosticConnection)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE recordings(id INTEGER PRIMARY KEY,identity_key TEXT UNIQUE)')
        conn.execute('CREATE TABLE rec_musical_profiles(identity_key TEXT PRIMARY KEY,version TEXT,digest TEXT)')
        conn.execute('CREATE TABLE measurements(' + ('model_version_id INTEGER' if normalized else 'model_version TEXT') + ')')
        conn.execute('CREATE TABLE vibe_versions(id INTEGER PRIMARY KEY,value TEXT)')
        version_column = 'inputs_version_id INTEGER' if normalized else 'inputs_version TEXT'
        conn.execute('CREATE TABLE recording_vibes(recording_id INTEGER,vibe TEXT,score REAL,'
                     'support TEXT,source TEXT,' + version_column + ')')
        versions = ['exact policy | café', 'another policy ' * 80]
        conn.executemany('INSERT INTO vibe_versions VALUES (?,?)', enumerate(versions, 1))
        expected_rows = {}
        for index in range(count):
            key = f'{index:04}|café'
            conn.execute('INSERT INTO recordings VALUES (?,?)', (index + 1, key))
            profile = (f'profile-{index % 2}', f'digest-{index}') if index % 3 else (None, None)
            if profile[0] is not None:
                conn.execute('INSERT INTO rec_musical_profiles VALUES (?,?,?)', (key, *profile))
            rows = []
            if index % 4:
                # Deliberately insert opposite to the required vibe order.
                for vibe, serial in [('relax', 1), ('concentrate', 0)]:
                    fit = (vibe, .125 + serial / 2, '{"support":0.8}', 'exact source')
                    conn.execute('INSERT INTO recording_vibes VALUES (?,?,?,?,?,?)',
                                 (index + 1, *fit, serial + 1 if normalized else versions[serial]))
                    rows.append((key, *profile, *fit, versions[serial]))
            expected_rows[key] = sorted(rows, key=lambda row: row[3]) or [(key, *profile, *([None] * 5))]
        keys = [*reversed(expected_rows), 'absent', *expected_rows]
        digest = hashlib.sha256()
        ordered = sorted(set(keys))
        for start in range(0, len(ordered), 512):
            rows = [row for key in ordered[start:start + 512] for row in expected_rows.get(key, ())]
            digest.update(json.dumps(rows, separators=(',', ':')).encode())
        repo = MusicalProfilesRepo(SimpleNamespace(conn=conn, _read_only=True, _lock=threading.RLock()))
        profiles = MusicalProfiles(repo, None, None, None, None)
        assert profiles.readiness_fingerprint(iter(keys)) == digest.hexdigest()
        if count:
            with conn:
                conn.execute("INSERT INTO rec_musical_profiles VALUES ('0000|café','new','changed')")
            assert profiles.readiness_fingerprint(keys) != digest.hexdigest()
