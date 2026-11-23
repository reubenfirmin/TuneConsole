"""Keep persisted readiness hashes compatible across query changes."""
from contextlib import closing
import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.core import vibe_versions
from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.repos.musical_profiles import MusicalProfilesRepo
from yt_playlist.controllers.musical_profiles import MusicalProfiles
from yt_playlist.rec.vibe_score_policy import score_patterns


@pytest.mark.parametrize('normalized', [False, True])
@pytest.mark.parametrize('count', [0, 3, 515])
def test_readiness_hash_retains_exact_legacy_page_bytes(tmp_path, normalized, count):
    with closing(sqlite3.connect(tmp_path / 'fingerprint.db', factory=DiagnosticConnection)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE recordings(id INTEGER PRIMARY KEY,identity_key TEXT UNIQUE,'
                     'musical_profile_version TEXT,musical_profile_digest TEXT)')
        conn.execute('CREATE TABLE measurements(' + ('model_version_id INTEGER' if normalized else 'model_version TEXT') + ')')
        conn.execute('CREATE TABLE vibe_versions(id INTEGER PRIMARY KEY,value TEXT)')
        version_column = 'inputs_version_id INTEGER' if normalized else 'inputs_version TEXT'
        conn.execute('CREATE TABLE recording_vibes(recording_id INTEGER,vibe TEXT,score REAL,'
                     'support TEXT,source TEXT,' + version_column + ',measurement_id INTEGER,prediction_id INTEGER,'
                     'artist_key TEXT,subgenre_key TEXT,recordings INTEGER,variance REAL)')
        vibe_versions.install_application_relations(conn)
        conn.execute('CREATE VIEW served_vibe_fits AS SELECT * FROM vibe_recording_fits')
        prefix = score_patterns()[0].removesuffix('*')
        versions = [prefix + 'exact policy | café', prefix + 'another policy ' * 80]
        conn.executemany('INSERT INTO vibe_versions VALUES (?,?)', enumerate(versions, 1))
        expected_rows = {}
        for index in range(count):
            key = f'{index:04}|café'
            profile = (f'profile-{index % 2}', f'digest-{index}') if index % 3 else (None, None)
            conn.execute('INSERT INTO recordings VALUES (?,?,?,?)', (index + 1, key, *profile))
            rows = []
            if index % 4:
                # Deliberately insert opposite to the required vibe order.
                for vibe, serial in [('relax', 1), ('concentrate', 0)]:
                    fit = (vibe, .125 + serial / 2, '{"support":0.8}', 'audio_measurement')
                    conn.execute('INSERT INTO recording_vibes VALUES (?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL)',
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
                conn.execute("UPDATE recordings SET musical_profile_version='new',musical_profile_digest='changed' "
                             "WHERE identity_key='0000|café'")
            assert profiles.readiness_fingerprint(keys) != digest.hexdigest()
