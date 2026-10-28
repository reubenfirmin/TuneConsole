import logging
import time
from dataclasses import dataclass
from typing import Callable

from yt_playlist.core import db_space

log = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Context:
    now: float


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable


def history(conn, registry):
    table = conn.execute("SELECT type FROM sqlite_master WHERE name='schema_migrations'").fetchone()
    if table is None:
        return 0
    columns = list(conn.execute("PRAGMA table_info(schema_migrations)"))
    if (table[0] != 'table' or [(r[1], r[2].upper(), r[3], r[5]) for r in columns] !=
            [('version', 'INTEGER', 0, 1), ('name', 'TEXT', 1, 0), ('applied_at', 'REAL', 1, 0)]):
        raise MigrationError("Incompatible schema_migrations layout")
    rows = list(conn.execute("SELECT version,name,applied_at FROM schema_migrations ORDER BY version"))
    if len(rows) > len(registry):
        raise MigrationError("Database schema is newer than this application")
    for row, migration in zip(rows, registry):
        if row[0] != migration.version or row[1] != migration.name or not isinstance(row[2], (int, float)):
            raise MigrationError("Migration history is not the supported contiguous prefix")
    return len(rows)


def validate_catalog(conn, version):
    from . import v008_recordings, v010_play_event_subjects
    if version >= 23:
        from . import v023_albums
        validator = v023_albums
    elif version >= 18:
        from . import v018_discovery_sources
        validator = v018_discovery_sources
    else:
        validator = v010_play_event_subjects if version >= 10 else v008_recordings
    validator.validate(conn)
    if version >= 11:
        from . import v011_takeout_coverage
        v011_takeout_coverage.validate(conn)
    if version >= 12:
        from . import v012_enrichment_retries
        v012_enrichment_retries.validate(conn)
    if version >= 13:
        from . import v013_provider_coverage
        v013_provider_coverage.validate(conn)
    if version >= 14:
        from . import v014_artwork_retries
        v014_artwork_retries.validate(conn)
    if version >= 15:
        from . import v015_album_shares
        v015_album_shares.validate(conn)
    if version >= 24:
        from . import v024_account_scope
        v024_account_scope.validate(conn)
    elif version >= 19:
        from . import v019_playlist_provenance
        v019_playlist_provenance.validate(conn)
    if version >= 20:
        from . import v020_metadata_selections
        v020_metadata_selections.validate(conn)
    if version >= 25:
        from . import v025_relationships
        v025_relationships.validate(conn)
    if version >= 26:
        from . import v026_listening_observations
        v026_listening_observations.validate(conn)
    if version >= 27:
        from . import v027_artist_identity_redirects
        v027_artist_identity_redirects.validate(conn)
    if version >= 28:
        from . import v028_discovery_representatives
        v028_discovery_representatives.validate(conn)
    if version >= 29:
        from . import v029_artist_provisional_names
        v029_artist_provisional_names.validate(conn)
    elif version >= 21:
        from . import v021_artists
        v021_artists.validate(conn)
    if version >= 30:
        from . import v030_account_scope_preservation
        v030_account_scope_preservation.validate(conn)
    if version >= 31:
        from . import v031_spotify_history
        v031_spotify_history.validate(conn)
    if version == 22:
        from . import v022_artist_references
        v022_artist_references.validate(conn)


def validate_version(conn, version):
    """Use the current contract at its floor; frozen validators serve older upgrades."""
    from . import current
    if version >= current.CONTRACT_FLOOR:
        from . import v062_album_aliases
        current.validate(conn, min(version, current.VERSION),
                         replaced=v062_album_aliases.REPLACED if version >= 62 else frozenset())
        if version >= 53:
            from . import v053_shared_policies
            v053_shared_policies.validate(conn)
        if 54 <= version < 61:
            from . import v054_audio_vibes
            v054_audio_vibes.validate(conn)
        if version >= 55:
            from . import v055_audio_vibe_labels
            v055_audio_vibe_labels.validate(conn)
        if version >= 56:
            from . import v056_audio_session_labels
            v056_audio_session_labels.validate(conn)
        if version >= 57:
            from . import v057_audio_vibe_runs
            v057_audio_vibe_runs.validate(conn)
        if 58 <= version < 61:
            from . import v058_predicted_vibes
            v058_predicted_vibes.validate(conn)
        if version >= 59:
            from . import v059_group_publication
            v059_group_publication.validate(conn)
        if version >= 60:
            from . import v060_group_build_expiry
            v060_group_build_expiry.validate(conn)
        if version >= 61:
            from . import v061_recording_vibes
            v061_recording_vibes.validate(conn)
        if version >= 62:
            v062_album_aliases.validate(conn)
        if version >= 63:
            from . import v063_proposal_cache
            v063_proposal_cache.validate(conn)
        if version >= 64:
            from . import v064_musical_link_retractions
            v064_musical_link_retractions.validate(conn)
        return
    from .v001_baseline import validate
    if version < 8:
        validate(conn, legacy=version == 0)
        return
    from . import v008_recordings, v023_albums, v024_account_scope, v025_relationships
    replaced = set(v008_recordings.REPLACED)
    for floor, module in ((23, v023_albums), (24, v024_account_scope), (25, v025_relationships)):
        if version >= floor:
            replaced.update(module.REPLACED)
    validate(conn, legacy=False, replaced=replaced)
    validate_catalog(conn, version)


def migrate(conn, *, registry=None, clock=time.time):
    """Apply one version per transaction; preserve earlier commits if a later version fails.

    Store serializes its connection; SQLite's write reservation serializes independent startups.
    DDL, data changes and the version record commit together; SQLite rolls back an
    interrupted version without copying the database. Tests may inject a registry.
    """
    production = registry is None
    if production:
        from .registry import MIGRATIONS
        registry = MIGRATIONS
    registry = tuple(registry)
    if [m.version for m in registry] != list(range(1, len(registry) + 1)) or len({m.name for m in registry}) != len(registry):
        raise MigrationError("Invalid migration registry")
    if conn.in_transaction:
        raise MigrationError("Cannot migrate with an outstanding caller transaction")
    fresh = None
    while True:
        try:
            conn.execute('BEGIN IMMEDIATE')
            current = history(conn, registry)
            if fresh is None:
                fresh = not bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone())
            if production and fresh and current == 0:
                from . import current as current_schema
                current_schema.bootstrap(conn, registry, clock())
                conn.commit()
                log.info('Initialized current schema at version %s', current_schema.VERSION)
                continue
            if production:
                validate_version(conn, current)
            if current == len(registry):
                conn.rollback()
                return
            migration = registry[current]
            if current == 0:
                conn.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL)')
            migration.apply(conn, Context(clock()))
            if not conn.in_transaction:
                raise MigrationError('Migration committed outside the runner')
            if production:
                validate_version(conn, migration.version)
            conn.execute('INSERT INTO schema_migrations VALUES (?,?,?)', (migration.version, migration.name, clock()))
            conn.commit()
            log.info('Applied migration %s: %s', migration.version, migration.name)
        except BaseException as exc:
            conn.rollback()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            version = locals().get('migration')
            label = f'{version.version} ({version.name})' if version else 'validation/locking'
            raise MigrationError(f'Database migration {label} failed: {exc}' + db_space.failure_hint(conn, exc)) from exc
