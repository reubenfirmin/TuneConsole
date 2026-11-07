"""Bounded SQL inputs for recommendation bundle preparation."""
from yt_playlist.core import group_content, recommendation_pools


class BundleInputs:
    """SQL adapter also usable by offline importers with a private connection."""
    def __init__(self, conn):
        self.conn = conn

    def supports_pool_revalidation(self):
        return bool(self.conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='recommendation_pool_revalidations' AND type='table'").fetchone())

    def recording_ids(self, keys):
        result = {}
        for start in range(0, len(keys), 500):
            batch = keys[start:start + 500]
            marks = ','.join('?' for _ in batch)
            result.update(self.conn.execute(f'SELECT identity_key,id FROM recordings WHERE identity_key IN ({marks})', batch))
        return result

    def policy_families(self, versions):
        result = {}
        for start in range(0, len(versions), 500):
            batch = versions[start:start + 500]
            marks = ','.join('?' for _ in batch)
            result.update(self.conn.execute('SELECT version,COALESCE(base_version,version) FROM recommendation_policies '
                f'WHERE version IN ({marks})', batch))
        return result

    def recent_pools(self, *, generations, limit):
        previous = {}
        for row in self.conn.execute(
                'SELECT m.surface,m.mode_key,m.purpose,p.id,p.pool_key,p.policy_version,'
                'p.group_version_id,p.input_fingerprint,p.purpose,p.eligible_count,p.content_digest '
                "FROM (SELECT id FROM recommendation_generations WHERE status!='retiring' ORDER BY id DESC LIMIT ?) g "
                'JOIN recommendation_surface_pools m ON m.generation_id=g.id '
                'JOIN recommendation_pools p ON p.id=m.pool_id WHERE p.complete=1 '
                'AND NOT EXISTS(SELECT 1 FROM recommendation_pool_retirements r WHERE r.pool_id=p.id) '
                'ORDER BY g.id DESC LIMIT ?', (generations, limit)):
            previous.setdefault(tuple(row[:3]), []).append(tuple(row[3:]))
        return previous

    def pool_matches(self, identity, prepared):
        return group_content.equal(recommendation_pools.stored_rows(self.conn, identity), recommendation_pools.rows(prepared))
