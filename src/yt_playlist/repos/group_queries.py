"""Compact group reads; the repository owns the pinned, file-retaining snapshot."""
from yt_playlist.core import group_documents, group_storage, policy_documents
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.evidence_ledger import canonical


def _policy(conn, version):
    value = policy_documents.configuration(conn, version)
    if value is None:
        raise ValueError('Missing retained musical group policy')
    return canonical(value).decode()


def _generation(conn, row):
    value = group_documents.read(row['metadata_filename'], row['metadata_digest'],
                                  row['metadata_bytes'], row['metadata_offset'], 'generation')
    value['recording_inputs'] = group_storage.inputs_for(conn, row['input_manifest_id'])
    return {'generation': row['external_key'], 'policy': _policy(conn, row['policy_version']),
            'fingerprint': row['input_fingerprint'], 'created_at': row['created_at'],
            'serial': row['input_serial'], 'payload': value}


def head_generation(conn):
    row = conn.execute('SELECT g.external_key FROM group_head h JOIN group_generations g '
                       'ON g.id=h.generation_id WHERE h.id=1').fetchone()
    return row[0] if row else None


def head_checkpoint(conn):
    row = conn.execute('SELECT g.external_key,g.input_serial,g.policy_version FROM group_head h '
                       'JOIN group_generations g ON g.id=h.generation_id WHERE h.id=1').fetchone()
    return {'generation': row[0], 'serial': row[1], 'policy': _policy(conn, row[2])} if row else None


def head(conn, *, members=False):
    row = conn.execute('SELECT g.* FROM group_head h JOIN group_generations g ON g.id=h.generation_id '
                       'JOIN group_publications p ON p.generation_id=g.id WHERE h.id=1').fetchone()
    if row is None:
        return None
    return {**_generation(conn, row), 'groups': definitions(conn, row['external_key'], members=members)}


def _definition(conn, row):
    value = group_documents.read(row['definition_filename'], row['definition_digest'],
                                  row['definition_bytes'], row['definition_offset'], 'definition')
    parent = conn.execute('SELECT x.external_version FROM generation_groups x '
                         'WHERE x.generation_id=? AND x.group_id=?',
                         (row['generation_id'], row['parent_group_id'])).fetchone()
    return {**value, 'group_id': row['external_key'], 'version': row['external_version'],
            'parent_version': parent[0] if parent else None}


_SELECT = ('SELECT v.*,t.external_key,x.external_version,x.generation_id FROM generation_groups x '
           'JOIN group_generations g ON g.id=x.generation_id '
           'JOIN group_publications p ON p.generation_id=g.id '
           'JOIN group_versions v ON v.id=x.group_version_id JOIN taste_groups t ON t.id=x.group_id ')


def definitions(conn, generation, *, members=False):
    result = []
    for row in conn.execute(_SELECT + 'WHERE g.external_key=? ORDER BY x.position', (generation,)):
        value = _definition(conn, row)
        if members:
            value['members'] = {key: (similarity, support) for key, similarity, support in conn.execute(
                'SELECT r.identity_key,m.similarity,m.support FROM group_members m '
                'JOIN recordings r ON r.id=m.recording_id WHERE m.group_version_id=?', (row['id'],))}
            value['members'].update({key: (similarity, support) for key, similarity, support in conn.execute(
                'SELECT raw_key,similarity,support FROM unresolved_group_members WHERE group_version_id=?', (row['id'],))})
        result.append(value)
    return result


def definition(conn, version, *, generation=None):
    # An alias can name shared content, but the caller's exact served alias must
    # actually occur in that generation before historical feedback is admitted.
    row = conn.execute(_SELECT + 'JOIN group_version_aliases a ON a.external_version=x.external_version '
        'WHERE x.external_version=? AND (g.external_key=? OR (? IS NULL AND g.id=a.first_generation_id))',
        (version, generation, generation)).fetchone()
    if row is None:
        return None
    created = conn.execute('SELECT external_key,created_at FROM group_generations WHERE id=?',
                           (row['generation_id'],)).fetchone()
    return {**_definition(conn, row), 'generation': created[0], 'created_at': created[1]}


def correction_definition(conn, version, *, generation=None):
    """The exact historical identity needed by a correction, without its diagnostic graph."""
    row = conn.execute('SELECT t.external_key group_id,x.external_version version,'
        'g.external_key generation,g.created_at,parent.external_version parent_version '
        'FROM generation_groups x JOIN group_generations g ON g.id=x.generation_id '
        'JOIN group_publications p ON p.generation_id=g.id '
        'JOIN group_version_aliases a ON a.external_version=x.external_version '
        'JOIN group_versions v ON v.id=x.group_version_id JOIN taste_groups t ON t.id=x.group_id '
        'LEFT JOIN generation_groups parent ON parent.generation_id=x.generation_id '
        'AND parent.group_id=v.parent_group_id '
        'WHERE x.external_version=? AND (g.external_key=? OR (? IS NULL AND g.id=a.first_generation_id))',
        (version, generation, generation)).fetchone()
    return dict(row) if row is not None else None


def learning_head(conn):
    """The published hierarchy used by response learning, without model inputs."""
    generation = head_generation(conn)
    if generation is None:
        return None
    rows = conn.execute('SELECT t.external_key group_id,x.external_version version,'
        'parent.external_version parent_version,v.legacy,v.leaf,v.definition_filename,'
        'v.definition_digest,v.definition_bytes,v.definition_offset FROM group_head h '
        'JOIN group_publications p ON p.generation_id=h.generation_id '
        'JOIN generation_groups x ON x.generation_id=h.generation_id '
        'JOIN taste_groups t ON t.id=x.group_id JOIN group_versions v ON v.id=x.group_version_id '
        'LEFT JOIN generation_groups parent ON parent.generation_id=x.generation_id '
        'AND parent.group_id=v.parent_group_id WHERE h.id=1 ORDER BY x.position')
    groups = []
    for row in rows:
        value = group_documents.read(row['definition_filename'], row['definition_digest'],
                                     row['definition_bytes'], row['definition_offset'], 'definition')
        groups.append({key: row[key] for key in ('group_id', 'version', 'parent_version', 'legacy', 'leaf')}
                      | {'genres': value.get('genres', [])})
    return {'generation': generation, 'groups': groups}


def leaf_members(conn, generation):
    parameters = (generation,)
    base = ('FROM generation_groups x JOIN group_generations g ON g.id=x.generation_id '
            'JOIN group_publications p ON p.generation_id=g.id JOIN group_versions v '
            'ON v.id=x.group_version_id ')
    result = dict(conn.execute('SELECT r.identity_key,x.external_version ' + base
        + 'JOIN group_members m ON m.group_version_id=v.id JOIN recordings r ON r.id=m.recording_id '
        'WHERE g.external_key=? AND v.leaf=1', parameters))
    result.update(conn.execute('SELECT m.raw_key,x.external_version ' + base
        + 'JOIN unresolved_group_members m ON m.group_version_id=v.id '
        'WHERE g.external_key=? AND v.leaf=1', parameters))
    return result


def assignment_head(conn):
    header = head_checkpoint(conn)
    if header is None:
        return None
    groups = []
    for version_id, external_version in conn.execute('SELECT v.id,x.external_version FROM generation_groups x '
            'JOIN group_head h ON h.generation_id=x.generation_id JOIN group_versions v ON v.id=x.group_version_id '
            'WHERE h.id=1 AND v.leaf=1 AND v.legacy=0 ORDER BY x.position'):
        reps = [key for key, in conn.execute('SELECT COALESCE(r.identity_key,p.raw_key) '
            'FROM group_representatives p LEFT JOIN recordings r ON r.id=p.recording_id '
            'WHERE p.group_version_id=? ORDER BY p.position', (version_id,))]
        groups.append({'version': external_version, 'rep_keys': reps})
    return {'generation': header['generation'], 'policy': header['policy'], 'groups': groups}


def _lineage(row):
    payload = group_documents.read(row['evidence_filename'], row['evidence_digest'],
                                    row['evidence_bytes'], row['evidence_offset'], 'lineage')
    return {'source': row['source_alias'], 'target': row['target_alias'], 'relation': row['relation'], 'payload': payload}


def successors(conn, version, *, limit=512):
    if type(limit) is not int or not 0 <= limit <= 8192:
        raise ValueError('Invalid musical group lineage page size')
    rows = conn.execute('SELECT l.* FROM group_lineage l WHERE l.source_alias=? AND EXISTS('
        'SELECT 1 FROM group_generation_lineage x JOIN group_publications p ON p.generation_id=x.generation_id '
        'WHERE x.source_alias=l.source_alias AND x.target_alias=l.target_alias) '
        'ORDER BY l.target_alias LIMIT ?', (version, limit + 1)).fetchall()
    if len(rows) > limit:
        raise WorkBudgetExceeded('Musical lineage exceeds learning work ceiling')
    return [_lineage(row) for row in rows]


def lineage(conn):
    return [_lineage(row) for row in conn.execute('SELECT l.* FROM group_lineage l WHERE EXISTS('
        'SELECT 1 FROM group_generation_lineage x JOIN group_publications p ON p.generation_id=x.generation_id '
        'WHERE x.source_alias=l.source_alias AND x.target_alias=l.target_alias) ORDER BY l.source_alias,l.target_alias')]


def legacy_versions(conn):
    return {row[0] for row in conn.execute('SELECT a.external_version FROM group_version_aliases a '
        'JOIN group_versions v ON v.id=a.group_version_id JOIN taste_groups t ON t.id=v.group_id '
        'WHERE t.legacy_mode_id IS NOT NULL AND EXISTS(SELECT 1 FROM generation_groups x '
        'JOIN group_publications p ON p.generation_id=x.generation_id WHERE x.external_version=a.external_version)')}


def canonical_version(conn, version):
    row = conn.execute('SELECT group_version_id FROM group_version_aliases WHERE external_version=?', (version,)).fetchone()
    return row[0] if row else None
