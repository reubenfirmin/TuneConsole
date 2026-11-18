"""Validate live identity access through explicit account and client capabilities."""
from dataclasses import dataclass

from yt_playlist.core.authentication import is_auth_error
from yt_playlist.core.bridge import BridgeError


@dataclass(frozen=True)
class IdentityCheck:
    identity_id: int
    label: str
    outcome: str


def invalid_brand_id(brand):
    return brand.startswith(('http:', 'https:', '@', 'UC')) or '/' in brand


class IdentityChecks:
    def __init__(self, accounts, clients, clear_authentication, logger):
        self.accounts, self.clients = accounts, clients
        self.clear_authentication, self.logger = clear_authentication, logger

    def check(self, account_id=None):
        identities = self.accounts.get_accounts()
        if account_id is not None:
            identities = [row for row in identities if row.id == account_id]
        # Reject malformed Brand IDs before constructing or calling providers.
        clients = {}
        if any(not invalid_brand_id(row.brand_account_id or '') for row in identities):
            try:
                clients = self.clients() or {}
            except Exception:  # noqa: BLE001 - setup remains usable during provider failure
                self.logger.exception('could not build identity clients for setup checks')
        return [IdentityCheck(row.id, row.label, self._check(row, clients)) for row in identities]

    def _check(self, identity, clients):
        brand = identity.brand_account_id or ''
        if invalid_brand_id(brand):
            return 'invalid_brand_id'
        client = clients.get(identity.id)
        if client is None:
            return 'unavailable'
        try:
            account = client.get_account_info()
            if not isinstance(account, dict) or not account.get('accountName'):
                raise ValueError('YouTube did not return authenticated account information')
            # Both live permissions must succeed; an empty library is valid.
            playlists = client.get_library_playlists(limit=1)
            if not isinstance(playlists, list):
                raise ValueError('YouTube did not return a playlist library')
        except BridgeError:
            return 'disconnected'
        except Exception as error:  # noqa: BLE001 - check remaining identities independently
            self.logger.debug('identity check failed for %s', identity.label, exc_info=True)
            expired = is_auth_error(error)
            if brand and (expired or 'HTTP 400' in str(error) or 'HTTP 404' in str(error)):
                return 'brand_access_denied'
            return 'auth_required' if expired else 'unexpected_response'
        self.clear_authentication(identity.id)
        return 'authenticated'
