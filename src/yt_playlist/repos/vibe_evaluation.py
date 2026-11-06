"""Read-only evaluation captures composed from the original evidence owners."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from threading import RLock
from types import SimpleNamespace

from yt_playlist.core import fao
from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping
from yt_playlist.core.db_handles import connect
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.repos.discovery import DiscoveryRepo
from yt_playlist.repos.musical_profiles import MusicalProfilesRepo
from yt_playlist.repos.prepared_content import PreparedContentRepo
from yt_playlist.repos.rec_query import RecQueryRepo
from yt_playlist.repos.vibe_models import training_manifest


def evaluation_profile_inputs(conn, recordings, *, max_bytes=32 * 1024 * 1024):
    """Freeze real profile inputs after masking each target's own evidence.

    Artist/release descriptions and categorical genre choices remain allowed.
    No recording-source profile, numeric selection/override, prepared target
    profile or previously predicted score crosses this boundary.
    """
    reader = SimpleNamespace(conn=conn, _lock=RLock(), _read_only=True)
    reader.rec_query = RecQueryRepo(reader)
    reader.discovery = DiscoveryRepo(reader)
    sources, content = MusicalProfilesRepo(reader), PreparedContentRepo(reader)
    identities, result, used = sorted(set(recordings)), {}, 0
    for start in range(0, len(identities), 32):
        check_stopping()
        batch = identities[start:start + 32]
        keys = dict(conn.execute('SELECT identity_key,id FROM recordings WHERE id IN ('
            + ','.join('?' for _ in batch) + ')', batch))
        if set(keys.values()) != set(batch):
            raise ValueError('Evaluation profile requires each original recording')
        inputs = sources.recording_inputs(list(keys), source_scopes=('artist', 'release'))
        for key, _revision, raw, _digest, _different in content.capture([(key, 0) for key in keys]):
            payload = json.loads(raw)
            for features in payload.values():
                if features is not None:
                    features['audio'] = {}
            profiles, overrides, truncated = inputs[key]
            value = {'content': payload,
                'sources': [source for source in profiles if source['scope'] in {'artist', 'release'}],
                'overrides': {field: value for field, value in overrides.items() if field == 'genre'},
                'truncated': truncated}
            used += len(canonical(value))
            if used > max_bytes:
                raise WorkBudgetExceeded('Frozen masked profile inputs exceed byte budget')
            result[str(keys[key])] = value
    return result


def evaluation_snapshot(path, vibes):
    """Freeze originals and masked profiles without Store/init or migrations.

    The manifest has no predicted scores, annotation targets or file locators.
    Profile files retain their ordinary bounded owner and retention fence. The
    SQL handle fences replacement throughout capture. Target prepared profiles
    and predictions are never reused as masked conditioning.
    """
    database = Path(path).resolve()
    with (fao.retained_reader(fao.root_for_database(database, None, 'profiles')),
          closing(connect(database.as_uri() + '?mode=ro', uri=True)) as conn):
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        rows = {vibe:training_manifest(conn, vibe, limit=32768, max_bytes=32 * 1024 * 1024) for vibe in vibes}
        profiles = evaluation_profile_inputs(conn,
            (row['recording'] for values in rows.values() for row in values))
        try:
            sizes = [dict(row) for row in conn.execute('SELECT name,sum(pgsize) AS bytes '
                'FROM dbstat WHERE name IN (\'measurements\',\'recording_vibes\',\'predictions\',\'vibe_versions\','
                "'prediction_model_generations','prediction_model_generation') GROUP BY name")]
        except sqlite3.OperationalError:
            sizes = None
        return {'rows': rows, 'profile_inputs': profiles,
                'source': str(database), 'sql_owner_bytes': sizes,
                'page_size': conn.execute('PRAGMA page_size').fetchone()[0],
                'page_count': conn.execute('PRAGMA page_count').fetchone()[0],
                'sqlite_version': sqlite3.sqlite_version}
