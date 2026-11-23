"""Resolve captured playlist targets for action recovery."""
from . import playlist_scopes


def target(conn, action_id, role, position, *, account=None, remote=None, membership=None):
    prior = conn.execute('SELECT * FROM action_targets WHERE action_id=? AND role=? AND position=?',
                         (action_id, role, position)).fetchone()
    if (prior and prior['resolution_reason'] == 'membership_snapshot_mismatch' and account is None
            and prior['unresolved_membership_ref_id'] == membership
            and prior['provider_playlist_id_snapshot'] == remote):
        return prior['id']
    retained = conn.execute('SELECT playlist_scope_id,account_id FROM playlists WHERE id=?',
                            (membership,)).fetchone() if membership is not None else None
    scope = None
    reason = None
    if retained and retained['playlist_scope_id'] is not None:
        owner = conn.execute('SELECT provider_playlist_id FROM playlist_scopes WHERE id=?',
                             (retained['playlist_scope_id'],)).fetchone()
        if (account is not None and account != retained['account_id']) or (remote is not None and remote != owner[0]):
            raise ValueError('Action target disagrees with its retained membership')
        account, remote, scope = retained['account_id'], owner[0], retained['playlist_scope_id']
    elif membership is not None:
        reason = 'missing_membership'
    if remote is None:
        remote = f'@unresolved-membership:{membership}'
    if not isinstance(remote, str) or not remote:
        raise ValueError('Action target requires its captured remote identifier')
    if account is not None and type(account) is not int:
        raise ValueError('Action account requires an integer identity')
    known = conn.execute('SELECT id FROM accounts WHERE id=?', (account,)).fetchone() if account is not None else None
    if not known:
        reason = reason or ('missing_account' if account is not None else 'unrecorded_account')
    elif scope is None and not remote.startswith('@unresolved-membership:'):
        scope = playlist_scopes.ensure(conn, remote, account)
    values = (known[0] if known else None, None if known else account, scope, remote,
              membership if retained else None, None if retained else membership, reason)
    columns = ('account_id', 'unresolved_account_id', 'playlist_scope_id', 'provider_playlist_id_snapshot',
               'membership_ref_id', 'unresolved_membership_ref_id', 'resolution_reason')
    if prior:
        if tuple(prior[column] for column in columns) != values:
            raise ValueError('A committed action target cannot change')
        return prior['id']
    return conn.execute('INSERT INTO action_targets(action_id,role,position,' + ','.join(columns) + ') '
                        'VALUES (?,?,?,?,?,?,?,?,?,?)', (action_id, role, position, *values)).lastrowid
