"""Connection-local revisions for optimistic publication of disposable read snapshots.

TEMP triggers follow real writes, including cascades and rollbacks. Nothing is added to the
persisted schema: these counters only need to live as long as this connection's readers. Other
connections are detected separately through SQLite's data_version.
"""
from contextlib import contextmanager
import sqlite3


# These built-in table-valued functions derive rows from their SQL arguments.
# Actual tables with these names still have ordinary revision guards.
DERIVED_TABLES = frozenset({'json_each', 'json_tree'})


_KEY_COLUMNS = {"settings": "key", "rec_proposals": "surface", "player_events": "kind",
                "rec_proposal_cache_heads": "surface", "rec_proposal_cache_builds": "cache_id",
                "rec_proposal_cache_chunks": "cache_id",
                "recordings": "identity_key", "youtube_items": "video_id",
                "catalog_entries": "recording_id", "track_sources": ("recording_id", "video_id", "id"),
                "unresolved_items": "entry_id",
                "entry_field_choices": "entry_id", "library_memberships": "entry_id",
                "discovery_memberships": "recording_id", "discovery_sources": "recording_id",
                "rec_content_pending": "identity_key", "prepared_recording_inputs": ("recording_id", "raw_key"),
                "rec_musical_sources": "source_key", "rec_musical_links": ("source_key", "recording_id"),
                "metadata_choice_heads": "entry_id",
                "metadata_runtime_choices": "entry_id", "metadata_runtime_recording_sources": "recording_id",
                "metadata_runtime_entry_sources": "entry_id", "metadata_runtime_recording_genres": "recording_id",
                "metadata_observations": "entry_id",
                "metadata_file_writes": ("id", "owner_key"),
                "native_metadata_observations": "id", "metadata_deliveries": "id",
                "published_metadata_head": "id", "published_metadata_destination_head": "id",
                "published_metadata_destination_native": "source_key",
                "published_metadata_destination_deliveries": "id",
                "rec_musical_profiles": "identity_key", "entry_artists": "entry_id", "track_artists": "entry_id",
                "recording_vibes": "recording_id", "sample_attributes": "recording_id",
                "measurements": "recording_id",
                "vibe_versions": "id",
                "vibe_definitions": "vibe",
                "choice_learning_heads": "surface", "choice_learning_publications": "id",
                "choice_learning_parameters": "publication_id", "choice_learning_exclusions": "publication_id",
                "choice_mode_estimates": "publication_id", "choice_group_estimates": "publication_id",
                "choice_group_opportunities": "publication_id", "choice_interest_contexts": "publication_id",
                "choice_interest_features": "publication_id", "choice_interest_tracks": "publication_id",
                "choice_album_interests": "publication_id", "choice_album_track_days": "publication_id",
                "choice_interest_mix_descriptors": "publication_id",
                "group_generations": "id", "group_publications": "generation_id",
                "generation_groups": "generation_id", "taste_groups": "id", "group_versions": "id",
                "group_version_aliases": "external_version", "group_members": "group_version_id",
                "recommendation_policies": "version", "recommendation_policy_bodies": "version"}
# Keep the applied durable preview trigger definitions on their original keys.
_SNAPSHOT_KEY_COLUMNS = {**_KEY_COLUMNS, 'playlists': 'playlist_scope_id',
                         'prepared_recording_inputs': 'recording_id',
                         'unresolved_preparation_inputs': 'raw_key',
                         'rec_musical_source_profiles': 'source_key',
                         'metadata_file_references': 'owner_key',
                         'collection_file_writes': ('metadata_owner_key', 'content_digest', 'file_address', 'display_key',
                                                    'chunk_generation_id'),
                         'playlist_scopes': 'provider_playlist_id',
                         'playlist_items': 'playlist_id',
                         'choice_learning_publications': 'surface',
                         'group_versions': ('id', 'generation_id', 'external_version')}
# Chunk generations and file addresses can have the same numeric ID.
_SNAPSHOT_KEY_NAMESPACES = {('collection_file_writes', 'chunk_generation_id'): 'chunk-generation:'}
# Keyed musical-link readers consume membership and verified classifier values. The
# delivery token fences incoming alternatives; changing it alone must not
# discard a model calculation. Unannotated reads still guard the complete row.
_KEY_COLUMN_EXCLUSIONS = {"rec_musical_links": {"delivery_id"}, "group_versions": {"evidence_holds"},
                          "rec_musical_sources": {"accepted_run", "accepted_at", "accepted_hash"},
                          "group_generations": {"is_active", "build_owner", "build_expires_at", "build_ready",
                                                "build_output_digest", "build_parent_id", "worker_checkpoint_at",
                                                "worker_input_serial", "evidence_holds"}}
# A declared catalog read consumes recording fields except enrichment clocks
# and the separately read AcousticBrainz estimates. Default reads guard the row.
# Excluding only these fields also guards newly added catalog columns.
_COLUMN_GROUP_EXCLUSIONS = {
    "recordings": {"catalog": {"first_enriched_at", "last_enriched_at", "musical_profile_prepared_at",
                                "acousticbrainz_estimates", "content_pending_position", "content_pending_revision"}},
    "rec_musical_profiles": {"definition": {"prepared_at"}},
}
_COLUMN_GROUP_INCLUSIONS = {
    "group_generations": {"selection": {"is_active"}},
    "recordings": {"identity": {"id", "identity_key", "redirect_recording_id"}},
    "taste_axes": {
        "weights": {"id", "kind", "genre_key", "family_key", "value", "weight", "weight_updated_at"},
        "mix_controls": {"id", "kind", "genre_key", "family_key", "value", "home_hidden", "lean",
                         "lean_updated_at", "lean_last_graduated_day"},
    },
    "metadata_runtime_state": {"format": {"id", "active"}},
    "prepared_recording_inputs": {"grouping": {
        "recording_id", "library_present", "library_genre", "library_year"}},
}
_SNAPSHOT_COLUMN_GROUP_INCLUSIONS = {
    **_COLUMN_GROUP_INCLUSIONS,
    'prepared_recording_inputs': {**_COLUMN_GROUP_INCLUSIONS['prepared_recording_inputs'],
                                  'repair': {'recording_id'}},
    'recommendation_generation_surfaces': {'selection': {'selected'}},
    'metadata_runtime_state': {**_COLUMN_GROUP_INCLUSIONS['metadata_runtime_state'],
                               'enrichment_queue': {'enrichment_caught_up_at'}},
    'playlists': {'recap': {'id', 'account_id', 'title', 'track_count', 'created_at', 'created_at_source',
                            'playlist_scope_id', 'is_current'}},
    'playlist_scopes': {'recap': {'id', 'provider_playlist_id', 'origin', 'adopted', 'recipe_revision'}},
    "artists": {"identity": {"id", "name", "provisional", "redirect_artist_id"}},
    "recordings": {
        **_COLUMN_GROUP_INCLUSIONS["recordings"],
        "content_profile": {"id", "identity_key", "musical_profile_version", "musical_profile_digest",
                            "musical_profile_publication", "musical_profile_size", "musical_profile_file_id"},
        "vibe_evidence": {"id", "mb_recording_id", "effective_mb_recording_id", "acousticbrainz_estimates"},
    },
}


def _identifier(value):
    return '"' + value.replace('"', '""') + '"'


def _literal(value):
    return "'" + value.replace("'", "''") + "'"


class Revisions:
    @staticmethod
    def unbind(conn):
        """Retire this connection's counters when its schema is replaced."""
        for name, in conn.execute("SELECT name FROM sqlite_temp_schema WHERE type='trigger' "
                                  "AND name LIKE 'snapshot_revision_%'").fetchall():
            # Preparing DDL can refresh main's schema and automatically remove
            # TEMP triggers whose tables an external migration just retired.
            conn.execute(f'DROP TRIGGER IF EXISTS temp.{_identifier(name)}')
        conn.execute('DROP TABLE IF EXISTS temp.snapshot_revisions')

    def __init__(self, conn):
        self.conn = conn
        # Batch TEMP DDL, releasing only the transaction this constructor owns.
        # Admission can retry a cancelled bind without leaking a transaction.
        owned = not conn.in_transaction
        try:
            # SQLite may open BEGIN's transaction before execute is interrupted.
            if owned:
                conn.execute('BEGIN')
            self._bind()
            if owned:
                conn.commit()
        except BaseException:
            if owned:
                conn.rollback()
            raise

    def _bind(self):
        conn = self.conn
        self.schema_version = conn.execute('PRAGMA main.schema_version').fetchone()[0]
        self.tables = set()
        self.views = {r[0] for r in conn.execute(
            "SELECT name FROM main.sqlite_schema WHERE type='view'").fetchall()}
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS snapshot_revisions ("
                     "table_name TEXT NOT NULL, record_key TEXT NOT NULL, revision INTEGER NOT NULL,"
                     "PRIMARY KEY(table_name,record_key)) WITHOUT ROWID")
        for row in conn.execute("SELECT name FROM main.sqlite_schema WHERE type='table' "
                                "AND name NOT LIKE 'sqlite_%'").fetchall():
            name = row[0]
            self.tables.add(name)
            columns = [r[1] for r in conn.execute(
                f"PRAGMA main.table_xinfo({_identifier(name)})").fetchall()]
            changed = " OR ".join(f"OLD.{_identifier(c)} IS NOT NEW.{_identifier(c)}"
                                  for c in columns)
            key_changed = " OR ".join(f"OLD.{_identifier(c)} IS NOT NEW.{_identifier(c)}"
                for c in columns if c not in _KEY_COLUMN_EXCLUSIONS.get(name, ()))
            groups = {group: [c for c in columns if c not in excluded]
                      for group, excluded in _COLUMN_GROUP_EXCLUSIONS.get(name, {}).items()}
            groups.update({group: [c for c in columns if c in included]
                           for group, included in _SNAPSHOT_COLUMN_GROUP_INCLUSIONS.get(name, {}).items()})
            for operation in ("INSERT", "UPDATE", "DELETE"):
                keys = ["''"]
                if name in _SNAPSHOT_KEY_COLUMNS:
                    columns_for_keys = (_KEY_COLUMNS[name] if name == 'prepared_recording_inputs'
                                        else _SNAPSHOT_KEY_COLUMNS[name])
                    if isinstance(columns_for_keys, str):
                        columns_for_keys = (columns_for_keys,)
                    sides = ("OLD", "NEW") if operation == "UPDATE" else ("OLD",) if operation == "DELETE" else ("NEW",)
                    for column in columns_for_keys:
                        if column in columns:
                            namespace = _SNAPSHOT_KEY_NAMESPACES.get((name, column), '')
                            prefix = _literal('key:' + namespace)
                            keys.extend(f'{prefix} || {side}.{_identifier(column)}' for side in sides)
                writes = "".join(
                    # Nullable references have no keyed consumer. Their whole
                    # owner revision still advances through the first key.
                    "INSERT INTO snapshot_revisions(table_name,record_key,revision) SELECT "
                    f"{_literal(name)},{key},total_changes()+1 WHERE {key} IS NOT NULL "
                    + (f"AND ({key_changed}) " if operation == 'UPDATE' and key != "''"
                       and name in _KEY_COLUMN_EXCLUSIONS else '') +
                    "ON CONFLICT(table_name,record_key) "
                    "DO UPDATE SET revision=excluded.revision;" for key in keys)
                condition = f" WHEN {changed}" if operation == "UPDATE" else ""
                trigger = _identifier(f"snapshot_revision_{name}_{operation.lower()}")
                conn.execute(f"CREATE TEMP TRIGGER IF NOT EXISTS {trigger} AFTER {operation} "
                             f"ON main.{_identifier(name)}{condition} BEGIN {writes} END")
                for group, group_columns in groups.items():
                    if not group_columns:
                        continue
                    if name == 'recommendation_generation_surfaces' and group == 'selection':
                        # Only an update can select or unselect an existing surface.
                        if operation != 'UPDATE' or 'selected' not in columns:
                            continue
                    group_changed = " OR ".join(
                        f"OLD.{_identifier(c)} IS NOT NEW.{_identifier(c)}"
                        for c in group_columns)
                    condition = f" WHEN {group_changed}" if operation == "UPDATE" else ""
                    if name == 'group_generations' and group == 'selection' and operation != 'UPDATE':
                        condition = ' WHEN ' + ('NEW' if operation == 'INSERT' else 'OLD') + '.is_active=1'
                    if name == 'prepared_recording_inputs':
                        if operation == 'UPDATE':
                            condition = f' WHEN ({group_changed}) AND (OLD.recording_id IS NOT NULL OR NEW.recording_id IS NOT NULL)'
                        else:
                            row = 'NEW' if operation == 'INSERT' else 'OLD'
                            condition = f' WHEN {row}.recording_id IS NOT NULL'
                    trigger = _identifier(f"snapshot_revision_{name}_{group}_{operation.lower()}")
                    conn.execute(f"CREATE TEMP TRIGGER IF NOT EXISTS {trigger} AFTER {operation} "
                                 f"ON main.{_identifier(name)}{condition} BEGIN "
                                 "INSERT INTO snapshot_revisions(table_name,record_key,revision) "
                                 f"VALUES ({_literal(name)},{_literal('group:' + group)},total_changes()+1) "
                                 "ON CONFLICT(table_name,record_key) "
                                 "DO UPDATE SET revision=excluded.revision; END")

    def capture(self, dependencies=None):
        """Read last-write serials, seeking only requested dependencies when supplied.

        Each trigger stamps its dependency with total_changes()+1. The trigger's
        own row write advances that connection counter, so the committed stamp
        is above every earlier snapshot's serial and at most the final counter.
        Rollback restores the dependency stamps; counter gaps are harmless.
        """
        if dependencies is None:
            rows = self.conn.execute(
                "SELECT table_name,record_key,revision FROM temp.snapshot_revisions")
            return {(r[0], r[1]): r[2] for r in rows}
        else:
            dependencies = sorted(set(dependencies))
            result = {}
            for start in range(0, len(dependencies), 400):
                batch = dependencies[start:start + 400]
                values = ','.join('(?,?)' for _ in batch)
                rows = self.conn.execute(
                    f"WITH requested(table_name,record_key) AS (VALUES {values}) "
                    "SELECT r.table_name,r.record_key,r.revision FROM requested q "
                    "JOIN temp.snapshot_revisions r "
                    "ON r.table_name=q.table_name AND r.record_key=q.record_key",
                    tuple(value for dependency in batch for value in dependency))
                result.update(((r[0], r[1]), r[2]) for r in rows)
            return result


class Reads:
    def __init__(self):
        self.dependencies = set()
        self._record_keys = {}
        self._captures = []
        self._authorized = set()

    def include(self, dependencies):
        self.dependencies.update(dependencies)
        for captured in self._captures:
            captured.update(dependencies)

    @contextmanager
    def capture(self):
        """Capture complete dependencies, including reads already seen by an outer calculation."""
        dependencies = set()
        self._captures.append(dependencies)
        self._authorized.clear()
        try:
            yield dependencies
        finally:
            self._captures.pop()
            self._authorized.clear()

    @contextmanager
    def records(self, table, keys, *, prefix="key:"):
        previous = self._record_keys.get(table)
        if prefix == 'key:' and previous is not None and not previous[1]:
            # The caller declared which columns it consumes. A nested lookup
            # by row key must not add dependencies on the other columns.
            yield
            return
        self._record_keys[table] = (tuple(prefix + key for key in keys), prefix == 'key:')
        self._authorized.clear()
        try:
            yield
        finally:
            if previous is None:
                self._record_keys.pop(table)
            else:
                self._record_keys[table] = previous
            self._authorized.clear()

    def authorize(self, operation, table, _column, database, _trigger):
        if database == "main":
            if operation in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                return sqlite3.SQLITE_DENY
            if operation == sqlite3.SQLITE_READ:
                scope = self._record_keys.get(table)
                if scope is not None:
                    keys, keyed = scope
                    token = table, 'keys'
                    if token not in self._authorized:
                        self.include({(table, key) for key in keys})
                        self._authorized.add(token)
                    if not keyed or _column not in _KEY_COLUMN_EXCLUSIONS.get(table, ()):
                        return sqlite3.SQLITE_OK
                    # A caller consuming bookkeeping needs its full guard;
                    # keyed membership counters deliberately omit it.
                    token = table, ''
                elif table == 'metadata_runtime_state' and _column in ('id', 'active'):
                    # Format detection reads this owner directly. File-GC cursor
                    # changes must not invalidate model calculations or rankings.
                    token = table, 'group:format'
                else:
                    token = table, ''
                # View expansion reports each column repeatedly. Avoid a
                # callback and newly allocated closure for every repeated read.
                # Scope changes clear this set, preserving nested captures and
                # exact-key dependencies even when the outer scope saw them.
                if token not in self._authorized:
                    self.include({token})
                    self._authorized.add(token)
        return sqlite3.SQLITE_OK


def setting_reads(store, keys):
    """Declare exact setting keys while preparing a read statement on a snapshot connection."""
    return keyed_reads(store, "settings", keys)


@contextmanager
def keyed_reads(store, table, keys, *, column=None):
    """Track exact keys; the query must constrain every consumed row to these keys."""
    if table not in _SNAPSHOT_KEY_COLUMNS:
        raise ValueError(f"No keyed revision counter for {table}")
    namespace = _SNAPSHOT_KEY_NAMESPACES[table, column] if column is not None else ''
    reads = getattr(store, "_snapshot_reads", None)
    if reads is None:
        yield
    else:
        with reads.records(table, tuple(namespace + str(key) for key in keys)):
            yield


@contextmanager
def column_group_reads(store, table, group):
    """Declare a known column group, including reads expanded through catalog views.

    SQLite authorizes every base column in a view even for SELECT identity_key,
    so column authorizer events cannot distinguish catalog inputs from timestamps.
    Only callers which consume the declared fields may use this scope.
    """
    if (group not in _COLUMN_GROUP_EXCLUSIONS.get(table, {})
            and group not in _SNAPSHOT_COLUMN_GROUP_INCLUSIONS.get(table, {})):
        raise ValueError(f"Unknown snapshot column group: {table}.{group}")
    reads = getattr(store, "_snapshot_reads", None)
    if reads is None:
        yield
    else:
        with reads.records(table, (group,), prefix="group:"):
            yield
