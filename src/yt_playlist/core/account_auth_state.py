"""Per-account expired-session state, without replacing another account's flag."""

KEY = 'auth_expired_identities'


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='account_auth_expirations'").fetchone() is not None


def legacy_map(value):
    """Validate the entire compatible map before a caller merges any flags."""
    if not isinstance(value, dict):
        raise ValueError('Legacy expired authentication must be an account map')
    result = {}
    for owner, label in value.items():
        if (not isinstance(owner, str) or not owner.isdecimal() or str(int(owner)) != owner
                or not 0 < int(owner) < 2**63 or not isinstance(label, str)):
            raise ValueError('Legacy expired authentication requires exact account IDs and labels')
        result[int(owner)] = label
    return result


def read(conn):
    return dict(conn.execute('SELECT COALESCE(account_id,unresolved_account_id),label '
                             'FROM account_auth_expirations'))


def flag(conn,account_id,label,*,legacy=False):
    from . import entity_names
    if type(account_id) is not int or account_id<1 or not isinstance(label,str):
        raise ValueError('Expired authentication requires an exact account and label')
    known = conn.execute(f'SELECT 1 FROM {entity_names.accounts(conn)} WHERE id=?',(account_id,)).fetchone() is not None
    if not known and not legacy:
        raise ValueError('Expired authentication requires a recorded account')
    # Reserve the writer before the independent upsert. Historical unknown IDs
    # remain explicit; they are never assigned to a different account.
    conn.execute('UPDATE account_auth_expirations SET label=label WHERE 0')
    if known:
        conn.execute('INSERT INTO account_auth_expirations(account_id,label) VALUES (?,?) '
                     'ON CONFLICT(account_id) WHERE account_id IS NOT NULL DO UPDATE SET label=excluded.label',
                     (account_id,label))
    else:
        conn.execute('INSERT INTO account_auth_expirations(unresolved_account_id,label) VALUES (?,?) '
                     'ON CONFLICT(unresolved_account_id) WHERE unresolved_account_id IS NOT NULL DO UPDATE SET label=excluded.label',
                     (account_id,label))


def clear(conn,account_id=None):
    if account_id is None:
        conn.execute('DELETE FROM account_auth_expirations')
    else:
        conn.execute('DELETE FROM account_auth_expirations WHERE account_id=? OR unresolved_account_id=?',
                     (account_id,account_id))


def import_legacy(conn,value):
    if not isinstance(value,dict):
        raise ValueError('Legacy expired authentication must be an account map')
    for owner,label in value.items():
        if not isinstance(owner,str) or not owner.isdecimal() or str(int(owner))!=owner:
            raise ValueError('Legacy expired authentication has no exact account ID')
        flag(conn,int(owner),label,legacy=True)
