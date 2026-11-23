"""SQL inventory, accounting and bounded deletion of recommendation policies."""
from yt_playlist.core import policy_bodies
from yt_playlist.core.radio_policy_roots import VERSIONS as RADIO_ROOTS
from yt_playlist.core.policy_impressions import live_reference
from yt_playlist.core.publication_files import HOME, MANIFESTS


def operational_protection():
    """Foreign-key owners in addition to immutable impressions."""
    owners = ('album_description_requests', 'group_generations', 'group_versions',
              'recommendation_generations', 'recommendation_pools')
    return ' AND '.join(
        f'NOT EXISTS(SELECT 1 FROM {table} r WHERE r.policy_version=p.version)'
        for table in owners
    ) + (f" AND NOT EXISTS(SELECT 1 FROM {MANIFESTS} h,json_each(h.value,'$.policy_roots') r "
         f"WHERE h.key GLOB '{HOME}*' AND r.value->>1=p.version)"
         ' AND NOT EXISTS(SELECT 1 FROM recommendation_surface_pools WHERE revalidated_policy_version=p.version)'
         f' AND p.version NOT IN ({RADIO_ROOTS})')


def _typed_protection():
    return 'NOT ' + live_reference('p.version') + ' AND ' + operational_protection()


def retained_closure(conn, before):
    """SQL closure of explicit owners and policies newer than the supplied cutoff."""
    retained = {r[0] for r in conn.execute('SELECT version FROM recommendation_policies p '
        'WHERE created_at>=? OR NOT (' + _typed_protection() + ')', (before,))}
    bases = set()
    for start in range(0, len(versions := sorted(retained)), 256):
        batch = versions[start:start + 256]
        bases.update(r[0] for r in conn.execute('SELECT base_version FROM recommendation_policies '
            'WHERE base_version IS NOT NULL AND version IN (' + ','.join('?' for _ in batch) + ')', batch))
    return {'recommendation_policies': retained, policy_bodies.VIEW: bases}


def history_candidates(conn, before, limit, *, versions=None):
    selected = '' if versions is None else ' AND p.version IN (' + ','.join('?' for _ in versions) + ')'
    return [dict(row) for row in conn.execute('SELECT p.* FROM recommendation_policies p WHERE created_at<? AND '
        + live_reference('p.version') + ' AND '
        + operational_protection() + selected + ' ORDER BY created_at,version LIMIT ?',
        (before, *(versions or ()), limit))]


def retirement_pending(conn, before):
    policies = conn.execute('SELECT 1 FROM recommendation_policies p WHERE created_at<? AND '
        + _typed_protection() + ' LIMIT 1', (before,)).fetchone()
    return policies is not None or bool(conn.execute(
        f'SELECT 1 FROM {policy_bodies.VIEW} b WHERE NOT EXISTS('
        'SELECT 1 FROM recommendation_policies p WHERE p.base_version=b.version) LIMIT 1').fetchone())


def retention_candidates(conn, before, *, limit, byte_limit):
    versions, size = [], 0
    for version, byte_size in conn.execute(
            'SELECT version,byte_size FROM recommendation_policies p WHERE created_at<? AND ' + _typed_protection() +
            ' ORDER BY created_at,version LIMIT ?', (before, limit)):
        versions.append(version)
        size += byte_size
        if size >= byte_limit:
            break
    return versions, len(versions) == limit or size >= byte_limit


def prune(conn, versions, *, limit, byte_limit):
    # Recheck actual impression and owner references at the write boundary.
    deleted = 0
    for start in range(0, len(versions), 256):
        batch = versions[start:start + 256]
        deleted += conn.execute('DELETE FROM recommendation_policies AS p WHERE version IN (' + ','.join('?' for _ in batch)
                                + ') AND ' + _typed_protection(), batch).rowcount
    bodies, size = [], 0
    body_query = (f'SELECT b.version,b.byte_size FROM {policy_bodies.VIEW} b WHERE NOT EXISTS('
                  'SELECT 1 FROM recommendation_policies p WHERE p.base_version=b.version) LIMIT ?')
    for version, byte_size in conn.execute(body_query, (limit,)):
        bodies.append(version)
        size += byte_size
        if size >= byte_limit:
            break
    policy_bodies.delete(conn, bodies)
    return {'policies_deleted': deleted, 'bodies_deleted': len(bodies)}
