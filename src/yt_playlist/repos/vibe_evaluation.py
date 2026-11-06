"""Read-only evaluation captures composed from the original evidence owners."""
from yt_playlist.core import file_paths, policy_documents
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from threading import RLock
from types import SimpleNamespace

from yt_playlist.core import audio_annotations, audio_vibe_metadata, catalog_keys, fao
from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping
from yt_playlist.core.db_handles import connect
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.repos.discovery import DiscoveryRepo
from yt_playlist.repos.musical_profiles import MusicalProfilesRepo
from yt_playlist.repos.prepared_content import PreparedContentRepo
from yt_playlist.repos.rec_query import RecQueryRepo
from yt_playlist.repos.vibe_models import conditioning_albums, training_manifest


def annotation_snapshot(conn, historical_videos=(), *, limit=10000, max_bytes=16 * 1024 * 1024):
    """Preserve typed judgments and exact identities; never infer negative votes.

    Samples are optional for evaluating metadata transfer. Resolve a playback's
    video through the current catalog, then recording redirects; title matching
    and previous prediction scores cannot establish recording identity.
    """
    rows = conn.execute('SELECT id,run_id,legacy_run_key,kind,taxonomy_version,revision,state,'
        'created_at,supersedes_id,resolution_status,unresolved_sample_count FROM audio_annotations '
        'WHERE sealed=1 ORDER BY created_at,id LIMIT ?', (limit + 1,)).fetchall()
    if len(rows) > limit:
        raise WorkBudgetExceeded('Annotation inventory exceeds bounded admission')
    revisions, videos, used = [], set(historical_videos), 0
    if len(videos) > limit:
        raise WorkBudgetExceeded('Historical label identities exceed bounded admission')
    for row in rows:
        check_stopping()
        item = dict(row)
        run = row['run_id'] or row['legacy_run_key']
        found = {value[0] for value in conn.execute('SELECT video_id FROM audio_vibe_runs WHERE run_id=? '
            'UNION SELECT video_id FROM sample_attributes WHERE run_id=?', (run, run)) if value[0]}
        item['videos'] = sorted(found)
        videos.update(found)
        item['labels'] = [value[0] for value in conn.execute('SELECT vibe FROM audio_annotation_vibes '
            'WHERE annotation_id=? ORDER BY position', (row['id'],))]
        item['sample_ids'] = [value[0] for value in conn.execute('SELECT sample_id FROM audio_annotation_samples '
            'WHERE annotation_id=? ORDER BY position', (row['id'],))]
        item['evaluation'] = audio_annotations.evaluation(conn, row['id'])
        used += len(canonical(item))
        if used > max_bytes or len(videos) > limit:
            raise WorkBudgetExceeded('Annotation inventory exceeds bounded admission')
        revisions.append(item)
    identities = {}
    for video in sorted(videos):
        row = conn.execute('SELECT recording_id FROM youtube_items WHERE video_id=?', (video,)).fetchone()
        if row is not None and row[0] is not None:
            identities[video] = catalog_keys.resolve_recording(conn, row[0])
    for item in revisions:
        recordings = {identities[video] for video in item['videos'] if video in identities}
        # An ambiguous run remains an explicit gap, even if one upload maps.
        item['recording'] = next(iter(recordings)) if len(recordings) == 1 and all(
            video in identities for video in item['videos']) else None
    return {'format': 'typed-vibe-annotations-1', 'revisions': revisions, 'video_recordings': identities}


def annotation_conditioning(conn, recordings):
    """Freeze target metadata even when a label has no original measurement."""
    reader = SimpleNamespace(conn=conn, _lock=RLock(), _read_only=True)
    queries = RecQueryRepo(reader)
    albums = conditioning_albums(conn, recordings)
    result = {}
    for recording in sorted(recordings):
        artists = [row[0] for row in conn.execute('SELECT DISTINCT a.artist_id FROM catalog_entries e '
            f'JOIN {queries.entry_artists_table} a ON a.entry_id=e.id WHERE e.recording_id=? '
            'ORDER BY a.artist_id LIMIT 17', (recording,))]
        genre, = conn.execute('SELECT genre_key FROM recordings WHERE id=?', (recording,)).fetchone()
        result[str(recording)] = {'recording': recording, 'artists': artists if len(artists) <= 16 else [],
            'genre': audio_vibe_metadata.subgenre({'genre': genre}, conn=conn), 'album': albums.get(recording)}
    return result


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


def evaluation_snapshot(path, vibes, *, annotations=False, historical_videos=()):
    """Freeze originals and masked profiles without Store/init or migrations.

    The manifest has no predicted scores, annotation targets or file locators.
    Profile files retain their ordinary bounded owner and retention fence. The
    SQL handle fences replacement throughout capture. Target prepared profiles
    and predictions are never reused as masked conditioning.
    """
    database = Path(path).resolve()
    with (policy_documents.retained_reader(file_paths.root_for_database(database, None, 'profiles')),
          closing(connect(database.as_uri() + '?mode=ro', uri=True)) as conn):
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        rows = {vibe:training_manifest(conn, vibe, limit=32768, max_bytes=32 * 1024 * 1024) for vibe in vibes}
        extra = {}
        recordings = {row['recording'] for values in rows.values() for row in values}
        if annotations:
            labels = annotation_snapshot(conn, historical_videos)
            targets = {row['recording'] for row in labels['revisions'] if row['recording'] is not None}
            extra = {'annotations': labels, 'annotation_conditioning': annotation_conditioning(conn, targets)}
            recordings.update(targets)
        profiles = evaluation_profile_inputs(conn, recordings)
        try:
            sizes = [dict(row) for row in conn.execute('SELECT name,sum(pgsize) AS bytes '
                'FROM dbstat WHERE name IN (\'measurements\',\'recording_vibes\',\'predictions\',\'vibe_versions\','
                "'prediction_model_generations','prediction_model_generation') GROUP BY name")]
        except sqlite3.OperationalError:
            sizes = None
        return {'rows': rows, 'profile_inputs': profiles, **extra,
                'source': str(database), 'sql_owner_bytes': sizes,
                'page_size': conn.execute('PRAGMA page_size').fetchone()[0],
                'page_count': conn.execute('PRAGMA page_count').fetchone()[0],
                'sqlite_version': sqlite3.sqlite_version}
