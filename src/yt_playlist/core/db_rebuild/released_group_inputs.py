"""Validate released group documents before canonical rebuild publication."""
import math

from .. import group_content, group_storage


OLD_TABLES = ('rec_group_assignments', 'rec_group_graph', 'rec_group_unknown', 'rec_group_lineage',
              'rec_group_members', 'rec_group_head', 'rec_group_builds', 'rec_group_publications',
              'rec_group_definitions', 'rec_group_identities', 'rec_group_generations')
GENERATION_BYTES = 64 * 1024 * 1024


def _prepared(conn, generation):
    key, policy, fingerprint, created, serial, payload = tuple(generation)
    _count, size = conn.execute('SELECT count(m.identity_key),COALESCE(sum(length(m.identity_key)+32),0) '
        'FROM rec_group_definitions d LEFT JOIN rec_group_members m USING(version) WHERE d.generation=?',
        (key,)).fetchone()
    definition_bytes = conn.execute('SELECT COALESCE(sum(length(CAST(payload AS BLOB))),0) '
                                    'FROM rec_group_definitions WHERE generation=?', (key,)).fetchone()[0]
    if size * 4 + definition_bytes * 4 + len(payload.encode()) * 4 > GENERATION_BYTES:
        raise ValueError('Musical group generation exceeds bounded migration memory')
    groups = []
    for version, group_id, parent, raw, legacy in conn.execute('SELECT d.version,d.group_id,d.parent_version,'
            'd.payload,i.legacy_mode_id FROM rec_group_definitions d JOIN rec_group_identities i USING(group_id) '
            'WHERE d.generation=? ORDER BY d.rowid', (key,)):
        groups.append({'version': version, 'group_id': group_id, 'parent_version': parent, 'payload': raw,
                       'legacy_mode_id': legacy, 'members': [tuple(row) for row in conn.execute(
                           'SELECT identity_key,similarity,support FROM rec_group_members WHERE version=? '
                           'ORDER BY identity_key', (version,))]})
    return {'generation': key, 'policy': policy, 'fingerprint': fingerprint, 'at': created,
            'serial': serial, 'payload': payload, 'groups': groups}


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Invalid musical group timestamp')


def _lineage(raw):
    value = group_content.document(raw, 'Musical group lineage')
    for field in ('old_share', 'new_share'):
        if value.get(field) is not None:
            group_content.score(value[field])
    if value.get('compatible') is not None and type(value['compatible']) is not bool:
        raise ValueError('Invalid musical group lineage compatibility')
    if value.get('overlap') is not None and (type(value['overlap']) is not int or value['overlap'] < 0):
        raise ValueError('Invalid musical group lineage overlap')
    return value


def _preflight(conn, resolve):
    incomplete_versions = set()
    for table in OLD_TABLES:
        if conn.execute(f'PRAGMA foreign_key_check({table})').fetchone() is not None:
            raise ValueError(f'Broken original musical group reference in {table}')
    for key, legacy, created in conn.execute('SELECT * FROM rec_group_identities'):
        if not isinstance(key, str) or not key or legacy is not None and type(legacy) is not int:
            raise ValueError('Invalid musical group identity')
        _time(created)
    for generation in conn.execute('SELECT * FROM rec_group_generations ORDER BY rowid'):
        prepared = _prepared(conn, generation)
        published = conn.execute('SELECT 1 FROM rec_group_publications WHERE generation=?',
                                 (prepared['generation'],)).fetchone() is not None
        build = conn.execute('SELECT ready FROM rec_group_builds WHERE generation=?',
                             (prepared['generation'],)).fetchone()
        if not published and build is None:
            raise ValueError('Unpublished musical group generation has no build lease')
        if (not isinstance(prepared['generation'], str) or not prepared['generation']
                or not isinstance(prepared['fingerprint'], str) or type(prepared['serial']) is not int
                or prepared['serial'] < 0):
            raise ValueError('Invalid musical group generation metadata')
        _time(prepared['at'])
        payload = group_content.document(prepared['payload'], 'Musical group generation')
        group_content.input_manifest(payload.get('recording_inputs', {}))
        group_content.document(prepared['policy'], 'Musical group policy')
        staged_digest = payload.get('_staging_digest')
        if staged_digest is not None and (not isinstance(staged_digest, str) or len(staged_digest) != 64
                or any(c not in '0123456789abcdef' for c in staged_digest)):
            raise ValueError('Invalid musical group staging digest')
        parent = payload.get('_staging_parent')
        if parent is not None and (not isinstance(parent, str) or not conn.execute(
                'SELECT 1 FROM rec_group_generations WHERE generation=?', (parent,)).fetchone()):
            raise ValueError('Missing musical group staging parent')
        parents = {group['version']: group.get('parent_version') for group in prepared['groups']}
        for version in parents:
            visited, parent = {version}, parents[version]
            while parent is not None:
                if parent in visited or parent not in parents:
                    raise ValueError('Invalid musical group parent graph')
                visited.add(parent)
                parent = parents[parent]
        for group in prepared['groups']:
            if not isinstance(group['version'], str) or not group['version']:
                raise ValueError('Invalid musical group external version')
            value = group_content.document(group['payload'], 'Musical group definition')
            unfinished = not published and not build[0]
            fields = group_storage.definition_fields(value, None if unfinished else group['members'])
            if not fields[2] and group['members']:
                raise ValueError('Unknown musical group membership has asserted members')
            if fields[2] and fields[4] != len(group['members']):
                if len(group['members']) > fields[4]:
                    raise ValueError('Unfinished musical group membership exceeds its declared size')
                incomplete_versions.add(group['version'])
            group_content.digest(group_content.prepared_rows(prepared, group, resolve))
    for source, target, relation, payload in conn.execute('SELECT * FROM rec_group_lineage'):
        _lineage(payload)
        if (not isinstance(source, str) or not source or not isinstance(target, str) or not target
                or relation not in ('continuation', 'split', 'merge', 'legacy')):
            raise ValueError('Invalid musical group lineage relation')
        if not all(conn.execute('SELECT 1 FROM rec_group_definitions WHERE version=?', (version,)).fetchone()
                   for version in (source, target)):
            raise ValueError('Missing musical group lineage version')
        if source in incomplete_versions or target in incomplete_versions:
            raise ValueError('Temporal musical group evidence refers to incomplete membership')
    for generation, owner, expires, ready in conn.execute('SELECT * FROM rec_group_builds'):
        if not isinstance(owner, str) or not owner or type(ready) is not int or ready not in (0, 1):
            raise ValueError('Invalid musical group build lease')
        _time(expires)
    for generation, left, right, weight, support in conn.execute('SELECT * FROM rec_group_graph'):
        if not isinstance(left, str) or not isinstance(right, str):
            raise ValueError('Invalid musical group graph key')
        group_content.score(weight)
        group_content.score(support)
    for key, generation, version, similarity, support, profile, prepared_at in conn.execute(
            'SELECT * FROM rec_group_assignments'):
        if not isinstance(key, str) or profile is not None and not isinstance(profile, str):
            raise ValueError('Invalid musical group assignment')
        if version is not None and not conn.execute('SELECT 1 FROM rec_group_definitions '
                'WHERE generation=? AND version=?', (generation, version)).fetchone():
            raise ValueError('Musical group assignment version belongs to another generation')
        if version in incomplete_versions:
            raise ValueError('Musical group assignment refers to incomplete membership')
        group_content.score(similarity)
        group_content.score(support)
        _time(prepared_at)
    for _generation, key, reason in conn.execute('SELECT * FROM rec_group_unknown'):
        if not isinstance(key, str) or not isinstance(reason, str):
            raise ValueError('Invalid musical group unresolved evidence')
