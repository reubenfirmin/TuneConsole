"""Restartable provider refresh progress, separate from scalar settings."""

def read(conn, provider):
    row = conn.execute('SELECT refresh_cursor cursor,refresh_cutoff cutoff,refresh_fingerprint fingerprint,'
                       'refresh_revision revision FROM enrichment_provider_config WHERE provider=? AND refresh_cursor IS NOT NULL',
                       (provider,)).fetchone()
    return dict(row) if row else None


def clear(conn, provider):
    conn.execute('UPDATE enrichment_provider_config SET refresh_cursor=NULL,refresh_cutoff=NULL,'
                 'refresh_fingerprint=NULL,refresh_revision=NULL WHERE provider=?', (provider,))


def start(conn, provider, cutoff, fingerprint):
    conn.execute('UPDATE enrichment_provider_config SET refresh_cursor=0,refresh_cutoff=?,refresh_fingerprint=?,'
                 'refresh_revision=COALESCE(refresh_revision,0)+1 WHERE provider=?', (cutoff,fingerprint,provider))


def advance(conn, provider, refresh, cursor):
    changed = conn.execute('UPDATE enrichment_provider_config SET refresh_cursor=?,refresh_revision=refresh_revision+1 '
                           'WHERE provider=? AND refresh_revision=? AND refresh_fingerprint=?',
                           (cursor,provider,refresh['revision'],refresh['fingerprint'])).rowcount
    if changed != 1:
        raise RuntimeError('Provider refresh changed before its page was published')
