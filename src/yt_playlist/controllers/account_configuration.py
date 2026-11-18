"""Account setup commands, including authentication reconciliation after publication."""
from yt_playlist.core.accounts import select_remaining_master


class IdentityMissing(ValueError):
    pass


class LastIdentity(ValueError):
    pass


def removal(accounts, account_id):
    removed = next((row for row in accounts if row.id == account_id), None)
    if removed is None:
        raise IdentityMissing('Account no longer exists.')
    remaining = [row for row in accounts if row.id != account_id]
    if not remaining:
        raise LastIdentity('Keep at least one identity.')
    main = select_remaining_master(remaining)
    rows = [{'id': row.id, 'label': row.label, 'credential_ref': row.credential_ref,
             'brand_account_id': row.brand_account_id, 'is_master': row.id == main.id} for row in remaining]
    return removed, main.id, rows


class AccountConfiguration:
    def __init__(self, accounts, runtime, clear_authentication, authentication):
        self.accounts, self.runtime = accounts, runtime
        self.clear_authentication, self.authentication = clear_authentication, authentication

    def save(self, identities):
        self.runtime.apply_setup(identities)
        active = {row.id for row in self.accounts.get_accounts()}
        for account_id in list(self.authentication()):
            if account_id not in active:
                self.clear_authentication(account_id)

    def remove(self, account_id):
        removed, main_id, rows = removal(self.accounts.get_accounts(), account_id)
        self.save(rows)
        return {'label': removed.label, 'main_identity_id': main_id,
                'identity_count': len(rows), 'auth_labels': list(self.authentication().values())}
