"""Durable playlist identities: shared provider lists and account-specific system lists."""
SYSTEM_IDS = frozenset({'LM','SE'})


def key(identity_id, provider_id):
    return f'@account:{identity_id}:{provider_id}' if provider_id in SYSTEM_IDS else provider_id


def resolve(conn, provider_id, identity_id=None):
    if provider_id in SYSTEM_IDS:
        if identity_id is None:
            owners = [r[0] for r in conn.execute('SELECT DISTINCT identity_id FROM playlists WHERE ytm_playlist_id=?', (provider_id,))]
            if len(owners)!=1:
                raise ValueError('Choose an account for this system playlist')
            identity_id = owners[0]
        return key(identity_id,provider_id)
    elif provider_id.startswith('@account:'):
        try:
            _,owner,remote = provider_id.split(':')
            owner = int(owner)
        except (ValueError,TypeError):
            raise ValueError('Invalid playlist reference') from None
        if remote not in SYSTEM_IDS or (identity_id is not None and owner!=identity_id):
            raise ValueError('Invalid playlist account')
        return key(owner,remote)
    elif provider_id.startswith('@legacy:'):
        if not conn.execute('SELECT 1 FROM playlist_scopes WHERE ref=?', (provider_id,)).fetchone():
            raise ValueError('Unknown legacy playlist reference')
        return provider_id
    return provider_id


def ensure(conn, provider_id, identity_id=None):
    ref = resolve(conn,provider_id,identity_id)
    if conn.execute('SELECT 1 FROM playlist_scopes WHERE ref=?', (ref,)).fetchone():
        return ref
    if ref.startswith('@account:'):
        _,owner,provider_id = ref.split(':')
        identity_id,kind = int(owner),'account'
    else:
        identity_id,kind = None,'shared'
    conn.execute('INSERT OR IGNORE INTO playlist_scopes VALUES (?,?,?,?)', (ref,provider_id,identity_id,kind))
    return ref
