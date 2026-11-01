import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
from pathlib import Path
import sqlite3
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
    rebuild_tables: bool = False


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


@lru_cache(maxsize=2)
def _predecessor_contract(version):
    """Validate installed predecessors using their frozen DDL before upgrading.

    Versions 32/33 have frozen full definitions. Later predecessors added DDL
    through v51, with separate validators for subsequent replacements. This
    path is never used to initialize or validate a current database.
    """
    from . import current
    from .registry import MIGRATIONS

    def statements():
        floor = min(version, 33)
        pending = ''
        for line in Path(__file__).with_name(f'schema_v{floor:03}.sql').read_text().splitlines(keepends=True):
            pending += line
            if sqlite3.complete_statement(pending):
                yield pending
                pending = ''
        if pending.strip():
            raise MigrationError('Incomplete predecessor schema definition')
        for migration in MIGRATIONS[33:version]:
            if migration.version in (43, 46):  # Only retired settings.
                continue
            if migration.version == 45:
                yield "CREATE INDEX ix_actions_delete_pending ON actions(id DESC) WHERE status='delete_pending'"
            else:
                yield from import_module(migration.apply.__module__).statements()

    return current._schema_contract(statements())


def validate_version(conn, version, *, foreign_keys=True, data=True):
    """Use the current contract at its floor; frozen validators serve older upgrades."""
    from . import current
    if (not foreign_keys or not data) and version < 79:
        raise ValueError('Only readable installed schemas can defer data validation')
    if version >= current.VERSION:
        current.validate(conn, foreign_keys=foreign_keys, data=data)
        return
    if version >= 92:
        current._validate(conn, current._schema_contract(current.statements(version=version)), version,
                          foreign_keys=foreign_keys, data=data)
        return
    if 86 <= version < 92:
        from . import v092_installed_genre_contract
        v092_installed_genre_contract.validate_predecessor(
            conn, version, foreign_keys=foreign_keys, data=data)
        return
    if version in (80, 81, 82, 83, 84, 85):
        from . import v082_recommendation_work, v083_home_offers, v084_album_note_requests, v085_genre_preferences
        # These versions only add independent owners to the installed v80
        # contract; old readable libraries need no historical reconstruction.
        added = v085_genre_preferences.OBJECTS if version < 85 else frozenset()
        if version < 84:
            added |= v084_album_note_requests.OBJECTS
        if version < 83:
            added |= v083_home_offers.OBJECTS
        if version < 82:
            added |= v082_recommendation_work.OBJECTS
        if version == 80:
            added |= {'playback_feedback_receipts'}
        expected = current._schema_contract(current.statements(version=85))
        current._validate(conn, expected, version, replaced=added,
                          foreign_keys=foreign_keys, data=data)
        return
    if version >= current.CONTRACT_FLOOR:
        from . import v062_album_aliases
        key_replacements = frozenset()
        if version >= 66:
            from . import v066_explicit_keys
            key_replacements = frozenset((*v066_explicit_keys.TABLES, 'rec_mood'))
        if version >= 69:
            key_replacements |= {'rec_prepared_content', 'ix_prepared_content_revision', 'ix_group_library_inputs'}
        if version >= 70:
            from . import v070_policy_documents
            key_replacements |= v070_policy_documents.REPLACED
        if version >= 72:
            from . import v072_compact_groups
            key_replacements |= frozenset(v072_compact_groups.OLD_TABLES)
        if version >= 74:
            key_replacements |= {'artist_credit_observations'}
        if version >= 76:
            key_replacements |= {'legacy_track_evidence', 'identity_state_archive', 'legacy_enrichment_attempts'}
        if version >= 77:
            key_replacements |= {'playlist_scopes', 'playlist_group', 'hidden_playlists', 'overlap_ignored', 'playlist_provenance',
                                 'album_library_revisions', 'album_library_snapshots', 'album_library_legacy_cohort',
                                 'album_library_memberships', 'saved_albums', 'quarantined_playlists'}
        if version >= 78:
            key_replacements |= {'identities', 'road_trip_recipes'}
        if version >= 79:
            key_replacements |= {'track_artists'}
        predecessor = min(version, 51)
        current._validate(conn, _predecessor_contract(predecessor), predecessor,
                          replaced=key_replacements | (v062_album_aliases.REPLACED if version >= 62 else frozenset()),
                          foreign_keys=foreign_keys, data=data)
        if version >= 53:
            from . import v053_shared_policies
            v053_shared_policies.validate(conn, replaced=key_replacements)
        if 54 <= version < 61:
            from . import v054_audio_vibes
            v054_audio_vibes.validate(conn)
        if 55 <= version < 67:
            from . import v055_audio_vibe_labels
            v055_audio_vibe_labels.validate(conn)
        if 56 <= version < 67:
            from . import v056_audio_session_labels
            v056_audio_session_labels.validate(conn)
        if version >= 57:
            from . import v057_audio_vibe_runs
            v057_audio_vibe_runs.validate(conn)
        if 58 <= version < 61:
            from . import v058_predicted_vibes
            v058_predicted_vibes.validate(conn)
        if 59 <= version < 72:
            from . import v059_group_publication
            v059_group_publication.validate(conn, replaced=key_replacements)
        if 60 <= version < 72:
            from . import v060_group_build_expiry
            v060_group_build_expiry.validate(conn)
        if version >= 61:
            from . import v061_recording_vibes
            vibe_replacements = frozenset({'audio_vibe_feedback'}) if version >= 67 else frozenset()
            if version >= 79:
                vibe_replacements |= {f'vibe_artists_{event}' for event in ('insert', 'update', 'delete')}
            v061_recording_vibes.validate(conn, replaced=vibe_replacements)
        if version >= 62:
            v062_album_aliases.validate(conn, replaced=frozenset({'saved_albums'}) if version >= 77 else frozenset(),
                                       data=data)
        if 63 <= version < 73:
            from . import v063_proposal_cache
            v063_proposal_cache.validate(conn)
        if version >= 64:
            from . import v064_musical_link_retractions
            v064_musical_link_retractions.validate(conn)
        if 65 <= version < 69:
            from . import v065_group_library_lookup
            v065_group_library_lookup.validate(conn)
        if version >= 66:
            from . import v068_canonical_embeddings
            retired = (v070_policy_documents.REPLACED if version >= 70 else frozenset())
            if version >= 72:
                retired |= frozenset(v072_compact_groups.OLD_TABLES)
            if version >= 74:
                retired |= {'artist_credit_observations'}
            if version >= 76:
                retired |= {'legacy_track_evidence', 'identity_state_archive', 'legacy_enrichment_attempts'}
            if version >= 77:
                retired |= {'playlist_group', 'hidden_playlists', 'overlap_ignored', 'playlist_provenance',
                            'album_library_revisions', 'album_library_snapshots', 'album_library_legacy_cohort',
                            'album_library_memberships'}
            if version >= 79:
                retired |= {'track_artists'}
            # The predecessor contract above already checks every foreign key
            # on this same transaction. Validate each owner's structure without
            # rescanning the full installed library for each historical module.
            v066_explicit_keys.validate(conn, replaced=retired | (frozenset(v068_canonical_embeddings.SOURCES)
                                                                 if version >= 68 else frozenset()),
                                       foreign_keys=False, data=data)
        if version >= 67:
            from . import v067_audio_annotations
            v067_audio_annotations.validate(conn)
        if version >= 68:
            v068_canonical_embeddings.validate(conn)
        if version >= 69:
            from . import v069_prepared_recording_inputs
            v069_prepared_recording_inputs.validate(conn)
        if version >= 70:
            v070_policy_documents.validate(conn)
        if version >= 71:
            from . import v071_album_descriptions
            v071_album_descriptions.validate(conn)
        if version >= 72:
            v072_compact_groups.validate(conn)
        if version >= 73:
            from . import v073_recommendation_publications
            v073_recommendation_publications.validate(conn)
        if version == 74:
            from . import v074_artist_credits
            v074_artist_credits.validate(conn)
        if version >= 75:
            from . import v075_metadata_deliveries
            v075_metadata_deliveries.validate(conn)
        if version >= 76:
            from . import v076_legacy_evidence_owners
            v076_legacy_evidence_owners.validate(conn, foreign_keys=False)
        if version >= 77:
            from . import v077_scope_account_state
            v077_scope_account_state.validate(conn, foreign_keys=False)
        if version >= 78:
            from . import v078_account_contracts
            v078_account_contracts.validate(conn, foreign_keys=False, data=data)
        if version >= 79:
            from . import v079_entry_artists
            v079_entry_artists.validate(conn)
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


def _validate_migration_structure(conn, version):
    # Older upgrade validators predate the split admission contract. Keep their
    # historical safety checks until their unsupported serving formats advance.
    if version < 79:
        validate_version(conn, version)
    else:
        validate_version(conn, version, foreign_keys=False, data=False)


def migrate(conn, *, registry=None, clock=time.time, defer_validation=False):
    """Admit installed owners and apply independent bounded schema changes.

    Store serializes its connection; SQLite's write reservation serializes independent startups.
    Fresh databases receive the current empty schema. A supplied historical
    registry explicitly exercises the frozen upgrade route, one version per
    transaction; ordinary startup never invokes its populated conversions.
    Routine advancement checks the installed structural contract. Full-library
    integrity auditing has a separate read-only entry point in migrations.audit.
    True means the installed layout still needs compatibility adapters. It does
    not request an automatic conversion or pause ordinary application writes.
    """
    production = registry is None
    if production:
        from .registry import MIGRATIONS
        registry = MIGRATIONS
    registry = tuple(registry)
    from .registry import MIGRATIONS
    frozen = registry == tuple(MIGRATIONS)
    registered_prefix = registry == tuple(MIGRATIONS[:len(registry)])
    if [m.version for m in registry] != list(range(1, len(registry) + 1)) or len({m.name for m in registry}) != len(registry):
        raise MigrationError("Invalid migration registry")
    if conn.in_transaction:
        raise MigrationError("Cannot migrate with an outstanding caller transaction")
    fresh = None
    checked_entry = False
    while True:
        migration = None
        restore_foreign_keys = None
        try:
            conn.execute('BEGIN' if production else 'BEGIN IMMEDIATE')
            current = history(conn, registry)
            if fresh is None:
                fresh = not bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone())
            if production:
                if not fresh and current >= 79:
                    # All these installed schemas have readable repository
                    # adapters. Inspect structure on one committed read snapshot;
                    # neither a writer reservation nor full data scans belong
                    # before HTTP admission, even when no migration is pending.
                    validate_version(conn, current, foreign_keys=False, data=False)
                    from . import regular
                    regular.validate(conn)
                    conn.rollback()
                    if not defer_validation:
                        regular.migrate(conn, clock=clock)
                    return current < len(registry)
                if not fresh:
                    raise MigrationError('Installed layouts before 79 require the explicit historical upgrade route')
                conn.rollback()
                conn.execute('BEGIN IMMEDIATE')
                if history(conn, registry) != current:
                    conn.rollback()
                    continue
            if production and fresh and current == 0:
                from . import current as current_schema
                current_schema.bootstrap(conn, registry, clock())
                conn.commit()
                fresh = False
                log.info('Initialized current schema at version %s', current_schema.VERSION)
                continue
            if current == len(registry):
                if frozen and not checked_entry:
                    _validate_migration_structure(conn, current)
                conn.rollback()
                return False
            if frozen and not checked_entry and (current or not fresh):
                _validate_migration_structure(conn, current)
            checked_entry = True
            migration = registry[current]
            if migration.rebuild_tables and conn.execute('PRAGMA foreign_keys').fetchone()[0]:
                # SQLite's table-replacement procedure requires disabling FK
                # actions before BEGIN, otherwise DROP can cascade-delete
                # unrelated children. Reacquire and revalidate after releasing
                # the first reservation; another startup may have advanced.
                conn.rollback()
                restore_foreign_keys = 1
                conn.execute('PRAGMA foreign_keys=OFF')
                conn.execute('BEGIN IMMEDIATE')
                if history(conn, registry) != current:
                    conn.rollback()
                    continue
                if frozen:
                    _validate_migration_structure(conn, current)
            if current == 0:
                conn.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL)')
            # Applied migrations may consult the versioned definition provider.
            # Their implicit lookup must not change when a later bootstrap
            # retires an object that their own historical upgrade still needs.
            from .current import migration_definitions
            with migration_definitions(migration.version):
                migration.apply(conn, Context(clock()))
            if not conn.in_transaction:
                raise MigrationError('Migration committed outside the runner')
            if migration.rebuild_tables and conn.execute('PRAGMA foreign_key_check').fetchone():
                raise MigrationError('Table replacement broke a foreign-key reference')
            if frozen and migration.version == len(registry):
                _validate_migration_structure(conn, migration.version)
            conn.execute('INSERT INTO schema_migrations VALUES (?,?,?)', (migration.version, migration.name, clock()))
            if registered_prefix:
                from . import regular
                regular.after_owner_upgrade(conn)
            conn.commit()
            log.info('Applied migration %s: %s', migration.version, migration.name)
        except BaseException as exc:
            conn.rollback()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            label = f'{migration.version} ({migration.name})' if migration else 'validation/locking'
            raise MigrationError(f'Database migration {label} failed: {exc}' + db_space.failure_hint(conn, exc)) from exc
        finally:
            if restore_foreign_keys is not None:
                conn.execute(f'PRAGMA foreign_keys={restore_foreign_keys}')
