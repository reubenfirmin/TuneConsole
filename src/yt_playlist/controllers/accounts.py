"""Account identity choices and compatible authentication-state updates."""
import json
from threading import RLock

from yt_playlist.core import account_auth_state
from yt_playlist.core.config import account_flag, validate_accounts


def select_accounts(configs, existing):
    """Match saved identities without assigning one remote owner's history to another."""
    validate_accounts([{'id': cfg.account_id, 'label': cfg.label,
        'credential_ref': cfg.credential_ref, 'brand_account_id': cfg.brand_account_id,
        'is_master': cfg.is_master} for cfg in configs])
    selected, used = [], set()
    for cfg in configs:
        row = next((r for r in existing if r['id'] == cfg.account_id), None) if cfg.account_id else None
        if cfg.account_id is not None and row is None:
            raise ValueError('Unknown identity ID')
        if row is not None and (row['brand_account_id'] or None) != (cfg.brand_account_id or None):
            raise ValueError('An existing account cannot be retargeted; add the other account separately')
        if row is None and cfg.account_id is None:
            row = next((r for r in existing if r['label'] == cfg.label
                        and (r['brand_account_id'] or None) == (cfg.brand_account_id or None)), None)
        if row is None and cfg.account_id is None:
            matches = [r for r in existing if r['credential_ref'] == cfg.credential_ref
                       and (r['brand_account_id'] or None) == (cfg.brand_account_id or None)
                       and r['id'] not in used]
            row = matches[0] if len(matches) == 1 else None
        iid = row['id'] if row else None
        if iid in used:
            raise ValueError('each account may only appear once')
        if iid is not None:
            used.add(iid)
        selected.append((cfg, iid))
    return selected, used


class Accounts:
    def __init__(self, repository, settings, *, paused, maintenance_accounts):
        self.repository, self.settings = repository, settings
        self.paused, self.maintenance_accounts = paused, maintenance_accounts
        self._lock = RLock()

    def auth_expirations(self):
        if self.repository.has_auth_state():
            return self.repository.auth_expirations()
        raw = self.settings.get_setting(account_auth_state.KEY)
        return account_auth_state.legacy_map(json.loads(raw)) if raw else {}

    def update_auth_expiration(self, *, account_id=None, label=None, clear_all=False):
        if self.paused():
            return None
        if not clear_all and label is not None:
            if type(account_id) is not int or account_id < 1 or not isinstance(label, str):
                raise ValueError('Expired authentication requires an exact account and label')
        with self._lock:
            if self.paused():
                return None
            if self.repository.has_auth_state():
                if clear_all or label is None:
                    current = self.repository.clear_auth_expirations(None if clear_all else account_id)
                else:
                    current = self.repository.flag_auth_expiration(account_id, label)
                return (current, False) if current is not None else None
            invalid = False
            try:
                current = self.auth_expirations()
            except (ValueError, TypeError):
                current, invalid = {}, True
            if clear_all:
                current.clear()
            elif label is not None:
                current[account_id] = label
            else:
                current.pop(account_id, None)
            if current:
                self.settings.set_setting(account_auth_state.KEY, json.dumps(current))
            else:
                self.settings.delete_setting(account_auth_state.KEY)
            return current, invalid

    def configured_ids(self, configs):
        configs = tuple(configs)
        existing = self.repository.account_rows(active=True)
        selected, used = select_accounts(configs, existing)
        if (any(iid is None for _, iid in selected) or used != {row['id'] for row in existing}
                or {iid for cfg, iid in selected if cfg.is_master}
                != {row['id'] for row in existing if row['is_master']}):
            raise ValueError('Account changes must wait for the storage update')
        return {cfg.label: iid for cfg, iid in selected}

    def upsert_account(self, label, credential_ref, brand_account_id, is_master):
        flag = account_flag(is_master)
        existing = self.repository.account_by_label(label)
        if existing and (existing['brand_account_id'] or None) != (brand_account_id or None):
            raise ValueError('An existing account cannot be retargeted; add the other account separately')
        return self.repository.upsert_account(label, credential_ref, brand_account_id, flag)

    def replace_accounts(self, configs):
        with self._lock:
            existing = self.repository.account_rows()
            selected, _ = select_accounts(tuple(configs), existing)
            rows = [(cfg.label, cfg.credential_ref, cfg.brand_account_id, int(cfg.is_master), iid)
                    for cfg, iid in selected]
            return self.repository.replace_accounts(rows, expected=existing)

    def get_accounts(self):
        if self.paused():
            cached = self.maintenance_accounts()
            if cached is not None:
                return list(cached)
        return self.repository.get_accounts()

    def get_master_account(self):
        for account in self.get_accounts():
            if account.is_master:
                return account
        raise ValueError('No master identity configured')
