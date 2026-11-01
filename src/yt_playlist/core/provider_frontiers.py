"""Restartable provider refresh progress, separate from scalar settings."""
import json

PREFIX = 'enrich_provider_refresh:'


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='provider_refresh_frontiers'").fetchone() is not None


def read(conn,provider):
    if available(conn):
        row = conn.execute('SELECT cursor,cutoff,fingerprint,revision FROM provider_refresh_frontiers WHERE provider=?',
                            (provider,)).fetchone()
        return dict(row) if row else None
    from .preparation.discovery_checkpoint_publication import OWNERS
    owner = OWNERS['provider-refresh']
    if (revision := owner.selected(conn)) is not None:
        row = owner.read(conn, revision, PREFIX+provider)
        return {field:row[field] for field in ('cursor','cutoff','fingerprint','revision')} if row else None
    row = conn.execute('SELECT value FROM settings WHERE key=?',(PREFIX+provider,)).fetchone()
    return json.loads(row[0]) if row else None


def clear(conn,provider):
    if available(conn):
        conn.execute('DELETE FROM provider_refresh_frontiers WHERE provider=?',(provider,))
    else:
        conn.execute('DELETE FROM settings WHERE key=?',(PREFIX+provider,))


def start(conn,provider,cutoff,fingerprint):
    if available(conn):
        conn.execute('INSERT INTO provider_refresh_frontiers(provider,cursor,cutoff,fingerprint,revision) '
            'VALUES (?,0,?,?,1) ON CONFLICT(provider) DO UPDATE SET cursor=0,cutoff=excluded.cutoff,'
            'fingerprint=excluded.fingerprint,revision=provider_refresh_frontiers.revision+1',
            (provider,cutoff,fingerprint))
    else:
        conn.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (PREFIX+provider,json.dumps(dict(cursor=0,cutoff=cutoff,fingerprint=fingerprint))))


def advance(conn,provider,refresh,cursor):
    if available(conn):
        changed = conn.execute('UPDATE provider_refresh_frontiers SET cursor=?,revision=revision+1 WHERE provider=? '
                               'AND revision=? AND fingerprint=?',
                               (cursor,provider,refresh['revision'],refresh['fingerprint'])).rowcount
        if changed != 1:
            raise RuntimeError('Provider refresh changed before its page was published')
    else:
        conn.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps({**refresh,'cursor':cursor}),PREFIX+provider))
