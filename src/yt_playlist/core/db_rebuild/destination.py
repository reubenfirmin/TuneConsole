"""Create the current schema once and import an explicitly admitted source.

The fresh destination has its own bootstrap history. The source's exact applied
history is retained separately as provenance; it is never advanced in place or
presented as a historical migration replay.
"""
from contextlib import closing
from functools import partial
import hashlib
from pathlib import Path
import sqlite3

from ..migrations import current, regular
from ..migrations.registry import MIGRATIONS
from .. import destination_schema, schema_bootstrap
from ..entity_names import PREDECESSOR_COLUMNS as COLUMNS
from . import archives, development, mix_history, mix_verification, optimized, released, runtime_history, runtime_retention, source, verification
from .build import Batch, Destination, Stage, _configure, quote


FORMAT = 1
PROVENANCE = ('CREATE TABLE database_rebuild_source_history ('
              'version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at REAL NOT NULL)')
REGULAR_PROVENANCE = ('CREATE TABLE database_rebuild_source_changes ('
                      'version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at REAL NOT NULL)')
TRIGGERS = '_database_rebuild_triggers'
INDEXES = '_database_rebuild_indexes'


def signature():
    """Fence the DDL, conversion code and file-owner code across resumes."""
    # Converters also call recommendation/repository and vocabulary owners.
    # A changed dependency cannot resume under the old candidate contract.
    root = Path(__file__).resolve().parents[2]
    result = hashlib.sha256(str(FORMAT).encode())
    result.update(destination_schema.DEFINITION.read_bytes())
    for path in sorted([*root.rglob('*.py'),*root.rglob('*.sql')]):
        result.update(str(path.relative_to(root)).encode())
        result.update(path.read_bytes())
    return 'normalized-'+str(current.VERSION)+'-'+result.hexdigest()


def initialize(target):
    from ..schema_inspection import reuse_tables

    # Initialization owns one private transaction. Reuse unchanged table
    # structure across bootstrap and bounded-change validators; changed CREATE
    # TABLE definitions are inspected again by the existing cache contract.
    with reuse_tables(target):
        _initialize(target)


def _initialize(target):
    target.row_factory = sqlite3.Row
    schema_bootstrap.bootstrap(target,MIGRATIONS,regular.CHANGES,0.,validation=current.validate)
    # Copy captured values without replaying live invalidation/capture effects.
    # Cross-owner domain checks run before the canonical guards are restored.
    target.execute('CREATE TABLE '+TRIGGERS+'(name TEXT PRIMARY KEY,sql TEXT NOT NULL) WITHOUT ROWID')
    for name,sql in target.execute("SELECT name,sql FROM main.sqlite_schema WHERE type='trigger'").fetchall():
        target.execute('INSERT INTO '+TRIGGERS+' VALUES (?,?)',(name,sql))
        target.execute('DROP TRIGGER '+quote(name))
    # Bulk insertion maintains only primary/unique identities. Build secondary
    # indexes once before domain readers and converters need them.
    target.execute('CREATE TABLE '+INDEXES+'(name TEXT PRIMARY KEY,sql TEXT NOT NULL) WITHOUT ROWID')
    for name,sql in target.execute("SELECT name,sql FROM main.sqlite_schema WHERE type='index' "
                                   "AND sql IS NOT NULL AND upper(sql) NOT LIKE 'CREATE UNIQUE%'").fetchall():
        target.execute('INSERT INTO '+INDEXES+' VALUES (?,?)',(name,sql))
        target.execute('DROP INDEX '+quote(name))
    target.execute(PROVENANCE)
    target.execute(REGULAR_PROVENANCE)


def _mapping(reader, table, target_columns):
    mapping = dict(COLUMNS.get(table,{}))
    from .. import vibe_versions
    if table in vibe_versions.COLUMNS:
        column = vibe_versions.COLUMNS[table]
        mapping[column] = column+'_id'
    if table == 'quarantined_playlists':
        mapping['ytm'] = 'playlist_scope_id'
    names = {name:mapping.get(name,name) for name in source.columns(reader,table)}
    if set(names.values()) != set(target_columns):
        # The release owner gained an empty current-publication pointer. Its
        # legacy track rows are converted by the album owner in a later stage.
        if table == 'albums' and set(target_columns)-set(names.values()) == {'current_tracklist_id'}:
            return names
        raise ValueError('Missing direct column mapping: '+table)
    return names


def _copy_rows(reader, target, cursor, limit, *, table, destination, mapping, retained=None,
               at=None, nulls=()):
    predicate = runtime_retention.plan(reader,at=at).get(table) if at is not None else None
    values, bookmark, done = source.scan(reader,table,cursor,limit,legacy_id='_legacy_id' in mapping,
                                         predicate=predicate)
    scanned = len(values)
    if retained is not None:
        values = [row for row in values if row['version'] in retained]
    if cursor is None:
        target.execute('DELETE FROM '+quote(destination))
    fields = ','.join(map(quote,mapping.values()))
    query = 'INSERT INTO '+quote(destination)+'('+fields+') VALUES ('+','.join('?' for _ in mapping)+')'
    from .. import vibe_versions
    version_column = vibe_versions.COLUMNS.get(table)
    target.executemany(query,[tuple(None if name in nulls else vibe_versions.intern(target,row[name]) if name == version_column else row[name]
                                  for name in mapping) for row in values])
    if table == 'predictions':
        from .. import vibe_provenance
        provenance = []
        for row in values:
            saved = vibe_provenance.read(reader, row['id'])
            if saved is not None:
                inputs = ({key:value for key,value in saved.items() if key != 'inputs'}
                          if 'generation' in saved else saved['inputs'])
                provenance.append((row['id'], inputs, saved['artists']))
        vibe_provenance.publish_many(target, provenance)
    return Batch(bookmark,scanned,done)


def _copy(reader, target, cursor, limit, *, table, destination, mapping, retained=None, at=None, nulls=()):
    from .. import vibe_versions
    if table in vibe_versions.COLUMNS or retained is not None or at is not None or nulls:
        return _copy_rows(reader,target,cursor,limit,table=table,destination=destination,
                          mapping=mapping,retained=retained,at=at,nulls=nulls)
    bookmark, count, done = source.window(reader,table,cursor,limit)
    if cursor is None:
        target.execute('DELETE FROM '+quote(destination))
    if count:
        keys = ','.join(map(quote,source.keys(reader,table)))
        marks = ','.join('?' for _ in bookmark)
        conditions, parameters = ['('+keys+')<=('+marks+')'], list(bookmark)
        if cursor is not None:
            conditions.append('('+keys+')>('+marks+')')
            parameters.extend(cursor)
        selected = ','.join(quote(source.keys(reader,table)[0] if name=='_legacy_id' else name)
                            for name in mapping)
        target.execute('INSERT INTO '+quote(destination)+'('+','.join(map(quote,mapping.values()))
            +') SELECT '+selected+' FROM rebuild_source.'+quote(table)+' WHERE '
            +' AND '.join(conditions)+' ORDER BY '+keys, parameters)
    return Batch(bookmark,count,done)


def indexes(reader, target, cursor, limit):
    values = target.execute('SELECT name,sql FROM '+INDEXES+' WHERE name>? ORDER BY name LIMIT ?',
                            (cursor or '',min(limit,8))).fetchall()
    for _,sql in values:
        target.execute(sql)
    if len(values)<min(limit,8):
        target.execute('DROP TABLE '+INDEXES)
        return Batch(rows=len(values),done=True)
    return Batch(values[-1][0],len(values),False)


def finish(reader, target, cursor, limit):
    """Restore a bounded set of guards, then remove private construction DDL."""
    values = target.execute('SELECT name,sql FROM '+TRIGGERS+' WHERE name>? ORDER BY name LIMIT ?',
                            (cursor or '',limit)).fetchall()
    for name,sql in values:
        existing = target.execute('SELECT sql FROM main.sqlite_schema WHERE name=?',(name,)).fetchone()
        if existing is None:
            target.execute(sql)
        elif current._tokens(existing[0]) != current._tokens(sql):
            raise ValueError('A domain writer changed the destination guard: '+name)
    if len(values)<limit:
        # Imported legacy actions have no send-admission checkpoint. Keep their
        # pending deletion attempts uncertain until ordinary provider readback.
        target.execute("UPDATE action_steps SET phase='uncertain',outcome='unknown' "
            "WHERE operation='delete_playlist' AND phase='pending' AND outcome='not_sent' "
            "AND action_id IN (SELECT id FROM actions WHERE idempotency_key='legacy-action:'||id)")
        target.execute('UPDATE prediction_id_state SET high_water=max(high_water,'
                       '(SELECT coalesce(max(id),0) FROM predictions)) WHERE id=1')
        # Preserve all retained auto-increment high-water marks, even when the
        # largest row was removed before the source snapshot.
        if reader.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence'").fetchone():
            for table,highwater in reader.execute('SELECT name,seq FROM sqlite_sequence'):
                name = 'accounts' if table=='identities' else table
                definition = target.execute('SELECT sql FROM sqlite_schema WHERE name=?',(name,)).fetchone()
                if definition and 'AUTOINCREMENT' in definition[0].upper():
                    if target.execute('SELECT 1 FROM sqlite_sequence WHERE name=?',(name,)).fetchone():
                        target.execute('UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?',(highwater,name))
                    else:
                        target.execute('INSERT INTO sqlite_sequence VALUES (?,?)',(name,highwater))
        target.execute('DROP TABLE '+TRIGGERS)
        return Batch(rows=len(values),done=True)
    return Batch(values[-1][0],len(values),False)


def for_source(path, *, split=False, legacy=False):
    """Select one of the two admitted input layouts; unknown owners fail closed."""
    path = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        version = source.admit(reader)
        from .. import metadata_runtime
        metadata_active = metadata_runtime.active(reader)
        # Inactive preparation is disposable staging, rather than selected
        # application state. Rebuild it from the captured installed owners;
        # stale partially prepared heads must not survive into the candidate.
        metadata_staging = ({name for name, sql in metadata_runtime.definitions() if sql.startswith('CREATE TABLE')}
                            | {'metadata_native_delivery_heads'}) if not metadata_active else set()
        captured_at = path.stat().st_mtime
        from ...repos import policy_storage
        from .. import policy_config
        policy_closure = policy_storage.retained_closure(reader, captured_at - policy_config.GRACE_SECONDS,
            protected=policy_config.protected_references(policy_storage.legacy_root_messages(reader))) or {}
        importer = released if version==source.RELEASE_VERSION else development
        sparse = split and not legacy
        version_signature = signature()
        # The app rebuild retains catalog, learning state and usable outputs.
        # Old diagnostic receipts are disposable; missing archive bodies must
        # not prevent the user from returning to their music.
        from .. import legacy_evidence
        diagnostics = (set(legacy_evidence.DOMAINS.values()) |
                       {'legacy_archive_heads', 'legacy_archive_cutovers'}) if split else set()
        # Current values and manual choices survive. Alternatives/conflicts can
        # be reacquired; restarting provider refresh also replaces old cursors.
        if split:
            diagnostics |= {'metadata_conflicts', 'metadata_conflict_candidates',
                            'metadata_runtime_conflicts', 'provider_refresh_frontiers'}
        if sparse:
            diagnostics |= runtime_retention.CHOICE_PROJECTIONS
            if version != source.RELEASE_VERSION:
                from .. import metadata_retention
                diagnostics |= metadata_retention.RETIRED
        with closing(sqlite3.connect(':memory:')) as empty:
            _configure(empty)
            empty.execute('BEGIN')
            initialize(empty)
            target_tables = source.tables(empty)
            originals = source.tables(reader)
            unknown = originals-target_tables-importer.CONVERTED-importer.DERIVED-{'identities'}
            if unknown:
                raise ValueError('Unclassified source owners: '+', '.join(sorted(unknown)))
            copies = []
            # An installed text layout may already own the additive dictionary.
            # Preserve it before interning versions from the three score owners.
            for table in sorted(originals-importer.CONVERTED-importer.DERIVED-metadata_staging-diagnostics-
                                {'prediction_inputs', 'prediction_artists'},
                                key=lambda table: (table != 'vibe_versions', table)):
                destination = {'identities':'accounts','schema_migrations':'database_rebuild_source_history',
                               'schema_changes':'database_rebuild_source_changes'}.get(table,table)
                if destination not in target_tables:
                    raise ValueError('Missing direct destination owner: '+table)
                mapping = _mapping(reader,table,source.columns(empty,destination))
                if source.keys(reader,table)[0] not in source.columns(reader,table) and (
                        source.keys(empty,destination)[0] not in source.columns(empty,destination)):
                    mapping = {'_legacy_id':source.keys(empty,destination)[0],**mapping}
                copies.append((table,destination,mapping))
            empty.rollback()

    def prepare(target):
        if signature()!=version_signature:
            raise ValueError('Destination code changed before initialization')
        initialize(target)

    def verify(reader, target):
        reader.row_factory = target.row_factory = sqlite3.Row
        if signature()!=version_signature:
            raise ValueError('Destination code changed during construction')
        current.validate(target)
        regular.validate(target)
        protected_groups = mix_verification.protected(reader, at=captured_at)
        for table,destination,mapping in copies:
            if table in mix_verification.REPLACED:
                continue
            if table in ('prediction_measurement_roots', 'prediction_provenance'):
                continue  # Populated by the prediction importer, checked below.
            if table in ('artist_credit_replays', 'artist_credit_retention', 'taste_mode_retention',
                         'taste_mode_retired_payloads', 'served_facts', 'served_fact_state'):
                # These additive owners can gain verified rows during this
                # build. Their exact captured facts and additions are checked
                # together by the domain adapter below.
                continue
            verification.retained(reader,target,table,destination,mapping,retained=policy_closure.get(table),
                                  at=captured_at,required=protected_groups.get(table))
        verification.predictions(reader,target)
        # No captured event may disappear or acquire a guessed membership.
        for table in ('play_events','listening_observations','actions'):
            expected = reader.execute('SELECT count(*) FROM '+table).fetchone()[0]
            actual = target.execute('SELECT count(*) FROM '+table).fetchone()[0]
            if expected != actual:
                raise ValueError('Direct rebuild changed captured history count: '+table)
        if target.execute('SELECT 1 FROM play_events WHERE membership_ref_id IS NOT NULL LIMIT 1').fetchone():
            raise ValueError('Direct rebuild invented an uncaptured listening membership')
        if version==source.RELEASE_VERSION:
            released.verify(reader,target)
        verification.converted(reader,target)
        optimized.verify(reader,target,at=captured_at,metadata_active=metadata_active)
        runtime_history.verify(reader,target,at=captured_at)
        mix_verification.verify(reader,target,at=captured_at)

    from .. import metadata_retention
    stages = tuple(Stage('copy:'+table,partial(_copy,table=table,destination=destination,mapping=mapping,
                                             retained=policy_closure.get(table),
                                             at=captured_at if sparse and version != source.RELEASE_VERSION else None,
                                             nulls=metadata_retention.NULLS.get(table,()) if sparse else ()),batch_size=8192)
                   for table,destination,mapping in copies)
    def profiles(reader,target,cursor,limit):
        from .. import profile_files
        count = sum(profile_files.prepare_batch(target,kind,min(limit,128)) for kind in profile_files.KINDS)
        return Batch((cursor or 0)+count,rows=count,done=count==0)
    def vectors(reader,target,cursor,limit):
        from .. import embedding_files
        count = sum(embedding_files.prepare_batch(target,kind,min(limit,128)) for kind in ('recording','artist'))
        return Batch((cursor or 0)+count,rows=count,done=count==0)
    def with_rows(stage):
        def copy(reader,target,cursor,limit,*prepared):
            # Resume opens new connections and can skip every earlier stage.
            reader.row_factory = sqlite3.Row
            connection = target.conn if stage.repositories else target
            connection.row_factory = sqlite3.Row
            return stage.copy(reader,target,cursor,limit,*prepared)
        return Stage(stage.name,copy,stage.repositories,stage.batch_size,stage.prepare)
    complete = Destination(version_signature,prepare,tuple(map(with_rows,
        (*stages,Stage('indexes',indexes),*importer.stages(),*optimized.stages(at=captured_at,metadata_active=metadata_active),
         *runtime_history.stages(captured_at),
         Stage('profiles',profiles),Stage('vectors',vectors),*archives.stages(captured_at),Stage('guards',finish),
         *mix_history.stages(captured_at)))),verify,compact=True)
    if not split:
        return complete

    # These owners feed model preparation. Their writers are the workers held
    # behind StorageMaintenance.ready, rather than ordinary application edits.
    # Parents precede children because the installed connection enforces FKs.
    deferred_tables = runtime_retention.DEFERRED if sparse else (
        'native_metadata_observations', 'musical_source_observations', *runtime_retention.DEFERRED)
    deferred = {stage.name: stage for stage in stages if stage.name[5:] in deferred_tables}
    runtime_copies = tuple(stage for stage in stages if stage.name not in deferred)
    imports = tuple(importer.stages(**({'sparse':True} if sparse and version == source.RELEASE_VERSION else {})))
    deferred_imports = tuple(stage for stage in imports
                             if stage.name.startswith('embedding:') or stage.name == 'released_learning_cleanup')
    runtime_imports = tuple(stage for stage in imports if stage not in deferred_imports
                            and stage.name != 'metadata:enrichment_conflict')
    runtime_metadata = tuple(stage for stage in optimized.stages(at=captured_at,
        metadata_active=metadata_active, native=False,direct=sparse and version != source.RELEASE_VERSION)
        if stage.name not in ('served-facts', 'metadata:providers', 'metadata:conflicts'))
    # Current native values are also used by foreground provider lookups. Their
    # documents and freshness must exist before activating runtime writers;
    # historical observations, profiles and embeddings remain deferred.
    native = optimized.native_stage(metadata_active=metadata_active)
    runtime_history_stages = runtime_history.stages(captured_at) if sparse else ()
    def verify_metadata(reader,target,cursor,limit):
        from ..preparation.metadata_runtime_verification import verify_selected, verify_native
        if not metadata_active:
            verify_selected(target,source=reader if version != source.RELEASE_VERSION else None)
            verify_native(target,reader)
        return Batch(done=True)
    retirement = (Stage('metadata:verify-runtime',verify_metadata),
                  Stage('metadata:retire-staging',runtime_retention.retired_metadata)) if sparse else ()
    receipts = (Stage('history:replay-identities',runtime_retention.receipts),) if sparse else ()
    runtime = (*runtime_copies, Stage('indexes',indexes), *runtime_imports, *receipts,
               native, *runtime_metadata,
               Stage('metadata:refresh',partial(optimized.refresh_metadata,at=captured_at)),
               *runtime_history_stages, *retirement,
               Stage('guards',finish))
    analytics = (*(deferred['copy:'+table] for table in deferred_tables if 'copy:'+table in deferred),
        *deferred_imports,
        # Compression is off-writer, but each durable rename still syncs its
        # directory. Keep that publication window small for queued edits.
        Stage('served-facts',optimized._served,batch_size=4,prepare=optimized.prepare_served),
        *(runtime_history.stages(captured_at) if not sparse else ()),
        Stage('profiles',profiles), Stage('vectors',vectors), *archives.stages(captured_at),
        *mix_history.stages(captured_at))

    def verify_runtime(reader, target):
        reader.row_factory = target.row_factory = sqlite3.Row
        current.validate(target)
        regular.validate(target)
        for table,owner,mapping in copies:
            if ('copy:'+table not in deferred
                    and table not in ('prediction_measurement_roots', 'prediction_provenance')
                    and not (sparse and table in metadata_retention.RETIRED)
                    and not (sparse and table in ('artist_credit_replays','artist_credit_retention',
                        'taste_mode_retention','taste_mode_retired_payloads'))):
                verification.retained(reader,target,table,owner,mapping,retained=policy_closure.get(table),
                    predicate=runtime_retention.plan(reader,at=captured_at).get(table)
                        if sparse and version != source.RELEASE_VERSION else None,
                    nulls=metadata_retention.NULLS.get(table,()) if sparse else (),
                    at=captured_at if sparse else None)
        verification.predictions(reader,target)
        verification.converted(reader,target)
        optimized.verify_refresh(target, at=captured_at)
        if not metadata_active:
            from ..preparation.metadata_runtime_verification import verify_selected
            if not sparse or version != source.RELEASE_VERSION:
                verify_selected(target,source=reader if sparse else None)
            from ..preparation.metadata_runtime_verification import verify_native
            verify_native(target,reader)
        if sparse:
            runtime_history.verify(reader,target,at=captured_at)
            if any(target.execute('SELECT 1 FROM '+quote(table)+' LIMIT 1').fetchone()
                   for table in metadata_retention.RETIRED):
                raise ValueError('Runtime rebuild retained obsolete metadata evidence')

    def verify_analytics(reader, target):
        reader.row_factory = target.row_factory = sqlite3.Row
        for table,owner,mapping in copies:
            if 'copy:'+table in deferred:
                verification.retained(reader,target,table,owner,mapping)
        # Native runtime documents were verified before cutover. Foreground
        # lookups may now advance those values and delivery heads normally.
        from .build import _checkpoint
        mix_verification.verify_captured(reader,target,inventory=_checkpoint(target).get('evidence'))

    return Destination(version_signature, prepare, tuple(map(with_rows, (*runtime,*analytics))), verify,
                       compact=sparse and version == source.RELEASE_VERSION,
                       runtime_stages=len(runtime), verify_runtime=verify_runtime,
                       verify_analytics=verify_analytics, capture=mix_verification.capture)
