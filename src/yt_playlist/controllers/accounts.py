"""Account identity choices and authentication-state updates."""
from threading import RLock

from yt_playlist.core.config import account_flag, validate_accounts


def select_accounts(configs, existing, *, importing=False):
    """Match saved identities without assigning one remote owner's history to another."""
    validate_accounts([{'id': cfg.account_id, 'label': cfg.label,
        'brand_account_id': cfg.brand_account_id,
        'is_master': cfg.is_master} for cfg in configs])
    selected, used = [], set()
    for cfg in configs:
        row = next((r for r in existing if r['id'] == cfg.account_id), None) if cfg.account_id else None
        if cfg.account_id is not None and row is None and not importing:
            raise ValueError('Unknown identity ID')
        if row is not None and (row['brand_account_id'] or None) != (cfg.brand_account_id or None):
            raise ValueError('An existing account cannot be retargeted; add the other account separately')
        if row is None and cfg.account_id is None:
            row = next((r for r in existing if r['label'] == cfg.label
                        and (r['brand_account_id'] or None) == (cfg.brand_account_id or None)), None)
        if row is None and cfg.account_id is None:
            matches = [r for r in existing
                       if (r['brand_account_id'] or None) == (cfg.brand_account_id or None)]
            row = matches[0] if len(matches) == 1 else None
        iid = row['id'] if row else cfg.account_id
        if iid in used:
            raise ValueError('each account may only appear once')
        if iid is not None:
            used.add(iid)
        selected.append((cfg, iid))
    return selected


class Accounts:
    def __init__(self, repository, *, paused):
        self.repository = repository
        self.paused = paused
        self._lock = RLock()

    def auth_expirations(self):
        return self.repository.auth_expirations()

    def update_auth_expiration(self, *, account_id=None, label=None, clear_all=False):
        if self.paused():
            return None
        if not clear_all and label is not None:
            if type(account_id) is not int or account_id < 1 or not isinstance(label, str):
                raise ValueError('Expired authentication requires an exact account and label')
        with self._lock:
            if self.paused():
                return None
            if clear_all or label is None:
                return self.repository.clear_auth_expirations(None if clear_all else account_id)
            return self.repository.flag_auth_expiration(account_id, label)

    def upsert_account(self, label, brand_account_id, is_master):
        flag = account_flag(is_master)
        existing = self.repository.account_by_label(label)
        if existing and (existing['brand_account_id'] or None) != (brand_account_id or None):
            raise ValueError('An existing account cannot be retargeted; add the other account separately')
        return self.repository.upsert_account(label, brand_account_id, flag)

    def replace_accounts(self, configs, *, importing=False):
        with self._lock:
            existing = self.repository.account_rows()
            selected = select_accounts(tuple(configs), existing, importing=importing)
            rows = [(iid, cfg.label, cfg.brand_account_id, int(cfg.is_master)) for cfg, iid in selected]
            return self.repository.replace_accounts(rows, expected=existing, importing=importing)

    def get_accounts(self):
        return self.repository.get_accounts()

    def bindings_saved(self):
        return self.repository.bindings_saved()

    def get_master_account(self):
        for account in self.get_accounts():
            if account.is_master:
                return account
        raise ValueError('No master identity configured')
