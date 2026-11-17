"""Durable playlist identities: shared provider lists and account-specific system lists."""
SYSTEM_IDS = frozenset({'LM','SE'})


def key(account_id, provider_playlist_id):
    return f'@account:{account_id}:{provider_playlist_id}' if provider_playlist_id in SYSTEM_IDS else provider_playlist_id


def resolve(conn, provider_playlist_id, account_id=None):
    if provider_playlist_id in SYSTEM_IDS:
        if account_id is None:
            owners = [r[0] for r in conn.execute('SELECT DISTINCT account_id FROM playlists '
                                                'WHERE provider_playlist_id=?', (provider_playlist_id,))]
            if len(owners)!=1:
                raise ValueError('Choose an account for this system playlist')
            account_id = owners[0]
        return key(account_id,provider_playlist_id)
    elif provider_playlist_id.startswith('@account:'):
        try:
            _,owner,remote = provider_playlist_id.split(':')
            owner = int(owner)
        except (ValueError,TypeError):
            raise ValueError('Invalid playlist reference') from None
        if remote not in SYSTEM_IDS or (account_id is not None and owner!=account_id):
            raise ValueError('Invalid playlist account')
        return key(owner,remote)
    elif provider_playlist_id.startswith('@legacy:'):
        if not conn.execute('SELECT 1 FROM playlist_scopes WHERE id=?', (provider_playlist_id,)).fetchone():
            raise ValueError('Unknown legacy playlist reference')
        return provider_playlist_id
    return provider_playlist_id


def ensure(conn, provider_playlist_id, account_id=None):
    playlist_scope_id = resolve(conn,provider_playlist_id,account_id)
    if conn.execute('SELECT 1 FROM playlist_scopes WHERE id=?', (playlist_scope_id,)).fetchone():
        return playlist_scope_id
    if playlist_scope_id.startswith('@account:'):
        _,owner,provider_playlist_id = playlist_scope_id.split(':')
        account_id,kind = int(owner),'account'
    else:
        account_id,kind = None,'shared'
    conn.execute('INSERT OR IGNORE INTO playlist_scopes(id,provider_playlist_id,account_id,kind) VALUES (?,?,?,?)',
                 (playlist_scope_id,provider_playlist_id,account_id,kind))
    return playlist_scope_id
