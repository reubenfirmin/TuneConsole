"""The runtime upgrade retains serving state, not obsolete preparation history.

All selection work is on the pinned read-only donor's TEMP schema. Original
choice evidence remains in the retained donor/immutable ledger; its compact
analytical projection is prepared after runtime admission. No source rows or
source ownership records are edited to obtain a smaller copy.
"""
from .. import credit_retention, metadata_retention
from . import build, mix_history, mix_verification, source


DEFERRED = ('rec_musical_profiles', 'rec_musical_source_profiles',
    'recording_embeddings', 'artist_embeddings',
    'recording_embedding_legacy_order', 'artist_embedding_legacy_order',
    'recording_profile_files', 'source_profile_files',
    'recording_embedding_files', 'artist_embedding_files')
CHOICE_PROJECTIONS = mix_history.LEARNING


def plan(reader, *, at):
    """Close current publications, operational groups, and replay identities."""
    exists = reader.execute("SELECT 1 FROM temp.sqlite_schema WHERE name='runtime_retention_plan'").fetchone()
    if exists:
        import json
        return dict((table, json.loads(value)) for table, value in reader.execute('SELECT * FROM runtime_retention_plan'))
    predicates = build.plan(reader, now=at, references=mix_verification.KEEP_OWNER)
    tables = source.tables(reader)
    if 'group_generations' in tables:
        predicates.update(_groups(reader, at, predicates))
    if 'credit_id' in source.columns(reader, 'artist_credit_observations'):
        predicates['artist_credit_observations'] = (
            "NOT (s.capture_kind IN ('legacy_capture','selection_snapshot') OR "
            "(s.capture_kind IN ('capture','provider_delivery') AND s.last_seen IS NOT NULL AND s.last_seen<"+repr(at-credit_retention.RETENTION_SECONDS)+')) '
            'OR EXISTS(SELECT 1 FROM artist_credit_capture_heads h WHERE h.observation_id=s.id) '
            'OR EXISTS(SELECT 1 FROM entry_artist_selections h WHERE h.observation_id=s.id)')
    import json
    reader.execute('CREATE TEMP TABLE runtime_retention_plan(owner TEXT PRIMARY KEY,predicate TEXT NOT NULL) WITHOUT ROWID')
    reader.executemany('INSERT INTO runtime_retention_plan VALUES (?,?)',
                       [(table, json.dumps(value)) for table, value in predicates.items()])
    return predicates


def _groups(reader, at, recommendations):
    # Seeds are actual current publications/builds and non-choice operational
    # owners. Historical choices retain their immutable menu/context evidence,
    # rather than pinning every old group and recommendation generation.
    for name in ('generations', 'aliases', 'versions', 'manifests', 'inputs'):
        kind = 'TEXT' if name == 'aliases' else 'INTEGER'
        reader.execute('CREATE TEMP TABLE runtime_group_'+name+'(id '+kind+' PRIMARY KEY) WITHOUT ROWID')
    reader.execute('CREATE TEMP TABLE runtime_group_edges(source_alias TEXT,target_alias TEXT,'
                   'PRIMARY KEY(source_alias,target_alias)) WITHOUT ROWID')
    reader.execute('INSERT OR IGNORE INTO runtime_group_generations '
        'SELECT generation_id FROM group_head UNION SELECT generation_id FROM group_evidence_generations WHERE '
        +mix_verification.KEEP_OWNER+' UNION SELECT group_generation_id FROM recommendation_generations s '
        'WHERE group_generation_id IS NOT NULL AND ('+recommendations['recommendation_generations']+') '
        "UNION SELECT generation_id FROM group_builds WHERE owner<>'cleanup' AND expires_at>=? "
        "UNION SELECT parent_generation_id FROM group_builds WHERE owner<>'cleanup' AND expires_at>=? "
        'AND parent_generation_id IS NOT NULL', (at, at))
    if 'group_preparation_state' in source.tables(reader):
        reader.execute('INSERT OR IGNORE INTO runtime_group_generations SELECT generation_id '
                       'FROM group_preparation_state WHERE generation_id IS NOT NULL')
    import json
    checkpoint = reader.execute("SELECT value FROM settings WHERE key='rec_group_shadow_checkpoint'").fetchone()
    if checkpoint and checkpoint[0]:
        generation = json.loads(checkpoint[0]).get('generation')
        reader.execute('INSERT OR IGNORE INTO runtime_group_generations SELECT id '
                       'FROM group_generations WHERE external_key=?', (generation,))
    reader.execute('INSERT OR IGNORE INTO runtime_group_aliases SELECT external_version '
                   'FROM group_evidence_versions WHERE '+mix_verification.KEEP_OWNER)
    # A referenced version can be shared by several aliases. Keep its actual
    # generation context as well as the exact alias identities used by readers.
    reader.execute('INSERT OR IGNORE INTO runtime_group_versions SELECT group_version_id '
        'FROM recommendation_pools s WHERE group_version_id IS NOT NULL AND ('
        +recommendations['recommendation_pools']+') UNION SELECT v.group_version_id '
        "FROM group_build_versions v JOIN group_builds b USING(generation_id) WHERE b.owner<>'cleanup' AND b.expires_at>=?", (at,))
    reader.execute('INSERT OR IGNORE INTO runtime_group_aliases SELECT external_version '
        'FROM group_version_aliases WHERE group_version_id IN (SELECT id FROM runtime_group_versions)')
    reader.execute('INSERT OR IGNORE INTO runtime_group_edges SELECT x.source_alias,x.target_alias '
        'FROM group_generation_lineage x JOIN group_builds b USING(generation_id) '
        "WHERE b.owner<>'cleanup' AND b.expires_at>=?", (at,))
    while True:
        before = reader.total_changes
        reader.execute('INSERT OR IGNORE INTO runtime_group_aliases SELECT external_version '
            'FROM generation_groups WHERE generation_id IN (SELECT id FROM runtime_group_generations)')
        reader.execute('INSERT OR IGNORE INTO runtime_group_edges SELECT l.source_alias,l.target_alias '
            'FROM group_lineage l JOIN runtime_group_aliases r ON r.id=l.source_alias WHERE EXISTS('
            'SELECT 1 FROM group_generation_lineage x JOIN group_publications p USING(generation_id) '
            'WHERE x.source_alias=l.source_alias AND x.target_alias=l.target_alias)')
        reader.execute('INSERT OR IGNORE INTO runtime_group_aliases SELECT source_alias FROM runtime_group_edges '
                       'UNION SELECT target_alias FROM runtime_group_edges')
        reader.execute('INSERT OR IGNORE INTO runtime_group_generations SELECT first_generation_id '
            'FROM group_version_aliases WHERE external_version IN (SELECT id FROM runtime_group_aliases) UNION '
            'SELECT min(x.generation_id) FROM group_generation_lineage x JOIN group_publications p USING(generation_id) '
            'JOIN runtime_group_edges e USING(source_alias,target_alias) GROUP BY x.source_alias,x.target_alias')
        if reader.total_changes == before:
            break
    reader.execute('INSERT OR IGNORE INTO runtime_group_versions SELECT group_version_id '
                   'FROM group_version_aliases WHERE external_version IN (SELECT id FROM runtime_group_aliases)')
    reader.execute('INSERT INTO runtime_group_manifests SELECT DISTINCT input_manifest_id FROM group_generations '
                   'WHERE id IN (SELECT id FROM runtime_group_generations)')
    reader.execute('INSERT OR IGNORE INTO runtime_group_inputs SELECT input_id FROM group_manifest_inputs '
        'WHERE manifest_id IN (SELECT id FROM runtime_group_manifests) UNION SELECT input_id FROM group_members '
        'WHERE group_version_id IN (SELECT id FROM runtime_group_versions) AND input_id IS NOT NULL')
    owners = {
        'group_generations': ('generations', 'id'), 'group_publications': ('generations', 'generation_id'),
        'generation_groups': ('generations', 'generation_id'), 'group_versions': ('versions', 'id'),
        'group_members': ('versions', 'group_version_id'), 'unresolved_group_members': ('versions', 'group_version_id'),
        'group_representatives': ('versions', 'group_version_id'), 'group_version_aliases': ('aliases', 'external_version'),
        'group_input_manifests': ('manifests', 'id'), 'group_manifest_inputs': ('manifests', 'manifest_id'),
        'unresolved_group_manifest_inputs': ('manifests', 'manifest_id'), 'group_recording_inputs': ('inputs', 'id')}
    for table in ('group_assignments', 'unresolved_group_assignments', 'group_unknown', 'unresolved_group_unknown',
                  'group_graph', 'unresolved_group_graph'):
        owners[table] = ('generations', 'generation_id')
    predicates = {table: 's.'+column+' IN (SELECT id FROM runtime_group_'+name+')'
                  for table, (name, column) in owners.items()}
    predicates['group_lineage'] = '(s.source_alias,s.target_alias) IN (SELECT * FROM runtime_group_edges)'
    predicates['group_generation_lineage'] = (predicates['group_lineage']+
        ' AND s.generation_id IN (SELECT id FROM runtime_group_generations)')
    for table in ('group_builds', 'group_build_versions'):
        predicates[table] = ("s.generation_id IN (SELECT generation_id FROM group_builds WHERE owner<>'cleanup' "
                             'AND expires_at>='+repr(at)+')')
    for table in mix_verification.ROOTS | {'home_display_group_roots', 'choice_learning_group_roots',
                                        'home_display_recommendation_roots'}:
        predicates[table] = mix_verification.KEEP_OWNER
    for table in mix_verification.SCRATCH - {'group_retention_state', 'group_retention_epoch'}:
        predicates[table] = '0'
    predicates['group_lineage_retirements'] = '0'
    return predicates


def receipts(reader, target, cursor, limit):
    """Known delivery retries must remain idempotent from runtime admission."""
    from .build import Batch
    if 'credit_id' not in source.columns(reader, 'artist_credit_observations'):
        return Batch(done=True)
    rows, cursor, done = source.scan(reader, 'artist_credit_observations', cursor, limit,
                                    predicate='s.delivery_key IS NOT NULL')
    for row in rows:
        credit_retention.prepare_receipt(target, row)
    return Batch(cursor, len(rows), done)


def retired_metadata(reader, target, cursor, limit):
    """Release temporary released-layout evidence before runtime admission.

    Development inputs are transformed straight from the donor and never copy
    these rows. The released importer may need temporary legacy observations.
    Guards are still suspended; each deletion commits with this bookmark.
    """
    from .build import Batch, quote
    if cursor is None:
        metadata_retention.plan(target)  # Includes the FK and replay-receipt audit.
        for table, columns in metadata_retention.NULLS.items():
            for column in columns:
                target.execute('UPDATE '+quote(table)+' SET '+quote(column)+'=NULL WHERE '+quote(column)+' IS NOT NULL')
        cursor = [0, 0]
    tables = sorted(metadata_retention.RETIRED)
    index, removed = cursor
    if index == len(tables):
        return Batch(done=True)
    table = tables[index]
    keys = source.keys(target, table)
    expression = ','.join(map(quote, keys))
    count = target.execute('DELETE FROM '+quote(table)+' WHERE ('+expression+') IN '
        '(SELECT '+expression+' FROM '+quote(table)+' LIMIT ?)', (limit,)).rowcount
    return Batch([index+int(count == 0), removed+count], count)
