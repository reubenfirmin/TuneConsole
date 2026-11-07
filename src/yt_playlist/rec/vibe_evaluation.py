"""Isolated read-only captures for offline vibe evaluation."""
from contextlib import closing
from pathlib import Path
import sqlite3
from threading import RLock
from types import SimpleNamespace

from yt_playlist.controllers.genres import Genres
from yt_playlist.controllers.musical_profiles import MusicalProfiles
from yt_playlist.controllers.prepared_content import PreparedContent
from yt_playlist.controllers.vibe_evaluation import VibeEvaluation
from yt_playlist.core import file_paths, policy_documents, profile_files
from yt_playlist.core.db_handles import connect
from yt_playlist.repos.discovery import DiscoveryRepo
from yt_playlist.repos.genres import GenreRepo
from yt_playlist.repos.musical_profiles import MusicalProfilesRepo
from yt_playlist.repos.prepared_content import PreparedContentRepo
from yt_playlist.repos.vibe_evaluation import VibeEvaluationRepo


def _evaluation(conn):
    reader = SimpleNamespace(conn=conn, _lock=RLock(), _read_only=True)
    discovery = DiscoveryRepo(reader)
    sources = MusicalProfiles(MusicalProfilesRepo(reader), profile_files.ProfileFiles(profile_files.directory(conn)),
                              None, None, None)
    return VibeEvaluation(VibeEvaluationRepo(reader), sources,
        PreparedContent(PreparedContentRepo(reader), discovery, None), Genres(GenreRepo(reader), discovery))


def annotation_snapshot(conn, historical_videos=(), **options):
    return _evaluation(conn).annotations(historical_videos, **options)


def annotation_conditioning(conn, recordings):
    return _evaluation(conn).conditioning(recordings)


def evaluation_profile_inputs(conn, recordings, **options):
    return _evaluation(conn).profile_inputs(recordings, **options)


def evaluation_snapshot(path, vibes, *, annotations=False, historical_videos=()):
    """Freeze originals and masked profiles without Store/init or migrations.

    This offline calculation owns its private read snapshot and file-reader
    lifetime. It publishes no database changes and never reuses target profiles
    or predictions as masked conditioning.
    """
    database = Path(path).resolve()
    with (policy_documents.retained_reader(file_paths.root_for_database(database, None, 'profiles')),
          closing(connect(database.as_uri() + '?mode=ro', uri=True)) as conn):
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        evaluation = _evaluation(conn)
        rows = {vibe: evaluation.training_manifest(vibe, limit=32768, max_bytes=32 * 1024 * 1024)
                for vibe in vibes}
        extra = {}
        recordings = {row['recording'] for values in rows.values() for row in values}
        if annotations:
            labels = annotation_snapshot(conn, historical_videos)
            targets = {row['recording'] for row in labels['revisions'] if row['recording'] is not None}
            extra = {'annotations': labels, 'annotation_conditioning': annotation_conditioning(conn, targets)}
            recordings.update(targets)
        return {'rows': rows, 'profile_inputs': evaluation_profile_inputs(conn, recordings),
                **extra, 'source': str(database), **evaluation.repository.storage_statistics()}
