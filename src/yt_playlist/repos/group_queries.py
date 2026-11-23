"""Detached SQL facts for compact group reads; no file reads or retention."""
from yt_playlist.core import policy_bodies


def _policy(conn, version):
    row = conn.execute('SELECT configuration_filename,content_digest,base_version,configuration_offset,byte_size '
                       'FROM recommendation_policies WHERE version=?', (version,)).fetchone()
    if row is None:
        return None
    body = None
    if row[2] is not None:
        reference = policy_bodies.read(conn, row[2])
        if reference is not None:
            body = (reference['configuration_filename'], reference['content_digest'],
                    reference['configuration_offset'], reference['byte_size'])
    return {'variant': tuple(row), 'body': body}


def head_generation(conn):
    row = conn.execute('SELECT external_key FROM group_generations WHERE is_active=1').fetchone()
    return row[0] if row else None


def head_checkpoint(conn):
    row = conn.execute('SELECT external_key,input_serial,policy_version '
                       'FROM group_generations WHERE is_active=1').fetchone()
    return {'generation': row[0], 'serial': row[1], 'policy': _policy(conn, row[2])} if row else None


def head(conn, *, members=False, generation_id=None):
    header = ('SELECT id,external_key,policy_version,input_fingerprint,input_serial,created_at,'
              'metadata_filename,metadata_digest,metadata_bytes,metadata_offset FROM group_generations ')
    row = (conn.execute(header + 'WHERE is_active=1').fetchone() if generation_id is None
           else conn.execute(header + 'WHERE id=? AND is_published=1', (generation_id,)).fetchone())
    if row is None:
        return None
    generation = {**dict(row), 'policy': _policy(conn, row['policy_version'])}
    return {**generation, 'groups': definitions(conn, row['external_key'], members=members)}


_SELECT = ('SELECT v.id,v.external_version,v.definition_filename,v.definition_digest,'
           'v.definition_bytes,v.definition_offset,t.external_key,parent.external_version parent_version,'
           'g.external_key generation,g.created_at generation_created_at '
           'FROM group_versions v '
           'JOIN group_generations g ON g.id=v.generation_id AND g.is_published=1 '
           'JOIN taste_groups t ON t.id=v.group_id '
           'LEFT JOIN group_versions parent ON parent.generation_id=v.generation_id '
           'AND parent.group_id=v.parent_group_id AND parent.position IS NOT NULL ')


def definitions(conn, generation, *, members=False):
    result = []
    for row in conn.execute(_SELECT + 'WHERE g.external_key=? AND v.position IS NOT NULL ORDER BY v.position', (generation,)):
        value = dict(row)
        value.pop('generation')
        if members:
            value['members'] = {key: (similarity, support) for key, similarity, support in conn.execute(
                'SELECT COALESCE(r.identity_key,m.raw_key),m.similarity,m.support FROM group_members m '
                'LEFT JOIN recordings r ON r.id=m.recording_id WHERE m.group_version_id=? '
                'ORDER BY m.recording_id IS NULL', (row['id'],))}
        result.append(value)
    return result


def definition(conn, version, *, generation=None):
    row = conn.execute(_SELECT +
        'WHERE v.external_version=? AND v.position IS NOT NULL '
        'AND (g.external_key=? OR (? IS NULL AND v.is_origin=1))',
        (version, generation, generation)).fetchone()
    if row is None:
        return None
    return {**dict(row), 'created_at': row['generation_created_at']}


def correction_definition(conn, version, *, generation=None):
    """The exact historical identity needed by a correction, without its diagnostic graph."""
    row = definition(conn, version, generation=generation)
    if row is None:
        return None
    return {'group_id': row['external_key'], 'version': row['external_version'],
            **{field: row[field] for field in ('generation', 'created_at', 'parent_version')}}


def learning_head(conn):
    """The published hierarchy used by response learning, without model inputs."""
    generation = head_generation(conn)
    if generation is None:
        return None
    rows = conn.execute('SELECT t.external_key group_id,v.external_version version,'
        'parent.external_version parent_version,v.legacy,v.leaf,v.definition_filename,'
        'v.definition_digest,v.definition_bytes,v.definition_offset FROM group_generations h '
        'JOIN group_versions v ON v.generation_id=h.id JOIN taste_groups t ON t.id=v.group_id '
        'LEFT JOIN group_versions parent ON parent.generation_id=h.id '
        'AND parent.group_id=v.parent_group_id AND parent.position IS NOT NULL '
        'WHERE h.is_active=1 AND v.position IS NOT NULL ORDER BY v.position')
    return {'generation': generation, 'groups': [dict(row) for row in rows]}


def member_rows(conn, query, generation, field, keys):
    if keys is None:
        yield from conn.execute(query, (generation,))
        return
    for offset in range(0, len(keys), 128):
        batch = keys[offset:offset + 128]
        placeholders = ','.join('?' for _ in batch)
        yield from conn.execute(f'{query} AND {field} IN ({placeholders})', (generation, *batch))


def leaf_members(conn, generation, *, keys=None):
    keys = list(dict.fromkeys(keys)) if keys is not None else None
    base = ('FROM group_versions v JOIN group_generations g ON g.id=v.generation_id AND g.is_published=1 ')
    result = dict(member_rows(conn, 'SELECT r.identity_key,v.external_version ' + base
        + 'JOIN group_members m ON m.group_version_id=v.id JOIN recordings r ON r.id=m.recording_id '
        'WHERE g.external_key=? AND v.leaf=1 AND v.position IS NOT NULL', generation, 'r.identity_key', keys))
    result.update(member_rows(conn, 'SELECT m.raw_key,v.external_version ' + base
        + 'JOIN group_members m ON m.group_version_id=v.id '
        'WHERE g.external_key=? AND v.leaf=1 AND v.position IS NOT NULL AND m.raw_key IS NOT NULL',
        generation, 'm.raw_key', keys))
    return result


def legacy_versions(conn):
    return {row[0] for row in conn.execute('SELECT v.external_version FROM group_versions v '
        'JOIN taste_groups t ON t.id=v.group_id JOIN group_generations p ON p.id=v.generation_id '
        'WHERE t.legacy_mode_id IS NOT NULL AND v.position IS NOT NULL AND p.is_published=1')}


def canonical_version(conn, version):
    row = conn.execute('SELECT v.id FROM group_versions v JOIN group_generations g ON g.id=v.generation_id '
                       'WHERE v.external_version=? AND v.is_origin=1 AND g.is_published=1', (version,)).fetchone()
    return row[0] if row else None
