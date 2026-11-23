"""Build a generation in bounded transactions, then publish it as a unit."""
import time
import uuid

from yt_playlist.core import group_storage
from yt_playlist.core.db_reads import read_sql_snapshot
from . import group_publication as publication

STAGED_TABLES = frozenset({'taste_groups', 'group_versions', 'group_members', 'recommendation_policies'})


def _touch(conn, generation_id, owner):
    if conn.execute('UPDATE group_generations SET build_expires_at=? WHERE id=? AND build_owner=? '
                    'AND build_ready=0 RETURNING id',
                    (time.time() + publication.LEASE_SECONDS, generation_id, owner)).fetchone() is None:
        raise RuntimeError('Musical group build is no longer owned by this worker')


def _existing(conn, prepared, policy_version):
    owner = prepared.setdefault('_stage_owner', uuid.uuid4().hex)
    row = conn.execute('SELECT g.*,p.external_key parent_key FROM group_generations g '
                       'LEFT JOIN group_generations p ON p.id=g.build_parent_id WHERE g.external_key=?',
                       (prepared['generation'],)).fetchone()
    if row is None:
        return None
    if row['is_published'] or row['build_owner'] is None:
        raise RuntimeError('Cannot resume a published musical group generation')
    if row['build_owner'] == publication._CLEANUP_OWNER:
        raise publication._BuildBeingCleaned
    actual = tuple(row[key] for key in ('policy_version', 'input_fingerprint', 'created_at', 'input_serial',
                                        'build_output_digest', 'parent_key'))
    expected = (policy_version, prepared['fingerprint'], prepared['at'], prepared['serial'],
                prepared['_stage_digest'], prepared.get('_stage_parent'))
    if actual != expected:
        raise RuntimeError('Musical group resume plan does not match its retained output digest')
    if row['build_owner'] != owner and row['build_expires_at'] > time.time():
        raise publication.BuildUnavailable('Musical group build is leased by another worker')
    return row


def generation_reference(conn, prepared, policy_version):
    row = _existing(conn, prepared, policy_version)
    if row is not None:
        return tuple(row['metadata_' + field] for field in ('filename', 'digest', 'bytes', 'offset'))
    return None


def conflicting_build(repo, prepared):
    with read_sql_snapshot(repo._store) as conn:
        for batch in publication._batches((group['version'],) for group in prepared['groups']):
            keys = [key for key, in batch]
            placeholders = ','.join('?' for _ in keys)
            row = conn.execute(f'SELECT g.external_key,g.build_expires_at FROM group_versions v '
                f'JOIN group_generations g ON g.id=v.generation_id WHERE v.external_version IN ({placeholders}) '
                'AND v.is_origin=1 AND g.is_published=0 AND g.external_key<>? LIMIT 1',
                (*keys, prepared['generation'])).fetchone()
            if row is not None:
                if row['build_expires_at'] is None or row['build_expires_at'] >= time.time():
                    raise publication.BuildUnavailable('Musical group version is leased by another unpublished build')
                return row['external_key']
    return None


def claim(repo, prepared, policy_version, metadata_reference):
    owner = prepared['_stage_owner']
    expires = time.time() + publication.LEASE_SECONDS
    with publication._write(repo) as conn:
        existing = _existing(conn, prepared, policy_version)
        if existing is not None:
            conn.execute('UPDATE group_generations SET build_owner=?,build_expires_at=? WHERE id=?',
                         (owner, expires, existing['id']))
            return existing['id'], owner, bool(existing['build_ready'])
        parent_id = None
        if (parent := prepared.get('_stage_parent')) is not None:
            row = conn.execute('SELECT id FROM group_generations WHERE external_key=? AND is_published=1',
                               (parent,)).fetchone()
            if row is None:
                raise ValueError('Missing published musical group staging parent')
            parent_id = row[0]
        generation_id = group_storage.install_header_reference(conn, prepared, policy_version, metadata_reference)
        conn.execute('UPDATE group_generations SET build_owner=?,build_expires_at=?,build_output_digest=?,'
                     'build_parent_id=? WHERE id=?',
                     (owner, expires, prepared['_stage_digest'], parent_id, generation_id))
        return generation_id, owner, False


def identities(repo, prepared, generation_id, owner):
    rows = ((g['group_id'], g.get('legacy_mode_id'), prepared['at']) for g in prepared['groups'])
    for batch in publication._batches(rows):
        with publication._write(repo) as conn:
            _touch(conn, generation_id, owner)
            for row in batch:
                conn.execute('INSERT INTO taste_groups(external_key,legacy_mode_id,created_at) VALUES (?,?,?) '
                             'ON CONFLICT(external_key) DO NOTHING', row)
                if conn.execute('SELECT legacy_mode_id FROM taste_groups WHERE external_key=?',
                                (row[0],)).fetchone()[0] != row[1]:
                    raise ValueError('Musical group identity changed its legacy owner')


def stages(repo, generation_id, owner):
    with read_sql_snapshot(repo._store) as conn:
        if conn.execute('SELECT 1 FROM group_generations WHERE id=? AND build_owner=? AND build_ready=0',
                        (generation_id, owner)).fetchone() is None:
            raise RuntimeError('Musical group build is no longer owned by this worker')
        return {row['external_key']: dict(row) for row in conn.execute(
            'SELECT t.external_key,v.id,v.external_version,v.complete,'
            '(SELECT count(*) FROM group_members m WHERE m.group_version_id=v.id) AS member_count '
            'FROM group_versions v JOIN taste_groups t ON t.id=v.group_id WHERE v.generation_id=?',
            (generation_id,))}


def install_definitions(repo, prepared, generation_id, owner, plans, files):
    staged = {}
    with publication._write(repo) as conn:
        _touch(conn, generation_id, owner)
        for plan, file in zip(plans, files, strict=True):
            position, group, _value, fields, digest = plan
            group_id, = conn.execute('SELECT id FROM taste_groups WHERE external_key=?',
                                     (group['group_id'],)).fetchone()
            version = group_storage.create_version(conn, prepared, group, generation_id, group_id, position, digest,
                                                   fields, file)
            staged[group['group_id']] = {'id': version, 'external_version': group['version'],
                                         'complete': 0, 'member_count': 0}
    return staged


def seal(repo, generation_id, owner, version_id):
    with publication._write(repo) as conn:
        _touch(conn, generation_id, owner)
        conn.execute('UPDATE group_versions SET complete=1 WHERE id=? AND generation_id=?', (version_id, generation_id))


def mark_ready(repo, generation_id, owner, count):
    with publication._write(repo) as conn:
        _touch(conn, generation_id, owner)
        if conn.execute('SELECT count(*) FROM group_versions WHERE generation_id=? AND complete=1',
                        (generation_id,)).fetchone()[0] != count:
            raise RuntimeError('Musical group staged selection is incomplete')
        conn.execute('UPDATE group_generations SET build_ready=1 WHERE id=?', (generation_id,))


def finish(conn, generation, owner):
    if not conn.in_transaction:
        raise RuntimeError('Group publication requires an outer transaction')
    row = conn.execute('SELECT id,is_published,build_owner,build_ready FROM group_generations WHERE external_key=?',
                       (generation,)).fetchone()
    if row is None:
        raise RuntimeError('Missing staged musical group generation')
    generation_id, published, lease, ready = row
    if published or lease != owner or not ready:
        raise RuntimeError('Musical group build is incomplete or no longer owned')
    conn.execute('UPDATE group_generations SET is_active=0 WHERE is_active=1')
    conn.execute('UPDATE group_generations SET is_published=1,is_active=1,published_at=?,'
                 'build_owner=NULL,build_expires_at=NULL,build_ready=0,build_output_digest=NULL,build_parent_id=NULL '
                 'WHERE id=?', (time.time(), generation_id))


def abandon(repo, prepared):
    if (owner := prepared.get('_stage_owner')) is not None:
        with publication._write(repo) as conn:
            conn.execute('UPDATE group_generations SET build_expires_at=0 WHERE build_owner=? AND external_key=?',
                         (owner, prepared['generation']))


def cleanup(repo, *, generation=None):
    """Fence an expired build and release a bounded prefix of its members."""
    with publication._write(repo) as conn:
        row = conn.execute('SELECT id FROM group_generations WHERE build_expires_at<? AND is_published=0 '
            + ('AND external_key=? ' if generation is not None else '') + 'ORDER BY build_expires_at LIMIT 1',
            (time.time(),) + ((generation,) if generation is not None else ())).fetchone()
        if row is None:
            return False
        generation_id, = row
        conn.execute('UPDATE group_generations SET build_owner=?,build_ready=0,build_expires_at=0,build_parent_id=NULL '
                     'WHERE id=?', (publication._CLEANUP_OWNER, generation_id))
        version = conn.execute('SELECT id FROM group_versions WHERE generation_id=? LIMIT 1',
                               (generation_id,)).fetchone()
        if version is None:
            conn.execute('DELETE FROM group_generations WHERE id=?', (generation_id,))
        elif not conn.execute('DELETE FROM group_members WHERE rowid IN (SELECT rowid FROM group_members '
                              'WHERE group_version_id=? LIMIT ?)', (*version, publication.BATCH_ROWS)).rowcount:
            conn.execute('DELETE FROM group_versions WHERE id=?', version)
        return True
