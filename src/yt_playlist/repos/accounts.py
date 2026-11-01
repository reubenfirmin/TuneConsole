"""AccountRepo: the YouTube Music accounts the app manages."""
from yt_playlist.repos.base import Repo, synchronized, lookup_read
from yt_playlist.repos.models import Account
from yt_playlist.core.config import account_flag, validate_accounts
from yt_playlist.core import entity_names
from contextlib import contextmanager
from uuid import uuid4


_UPSERT = (
    "INSERT INTO {table}(label, credential_ref, brand_account_id, is_master, active) "
    "VALUES (?,?,?,?,1) ON CONFLICT(label) DO UPDATE SET "
    "credential_ref=excluded.credential_ref, brand_account_id=excluded.brand_account_id, "
    "is_master=excluded.is_master, active=1"
)


def _select(configs, existing):
    """Resolve saved account identity without changing an existing remote owner."""
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
        iid = row['id'] if row else cfg.account_id
        if iid in used:
            raise ValueError('each account may only appear once')
        if iid is not None:
            used.add(iid)
        selected.append((cfg, iid))
    return selected, used


class AccountRepo(Repo):
    @lookup_read
    def configured_ids(self, configs):
        """Load existing account bindings during maintenance without reconciliation."""
        table = entity_names.owner(self.conn, 'accounts').table
        existing = list(self.conn.execute(f'SELECT * FROM {table} WHERE active=1'))
        selected, used = _select(tuple(configs), existing)
        if any(iid is None for _, iid in selected) or used != {r['id'] for r in existing}:
            raise ValueError('Account changes must wait for the storage update')
        masters = {r['id'] for r in existing if r['is_master']}
        if {iid for cfg, iid in selected if cfg.is_master} != masters:
            raise ValueError('Account changes must wait for the storage update')
        return {cfg.label: iid for cfg, iid in selected}

    @synchronized
    def upsert_account(self, label, credential_ref, brand_account_id, is_master):
        is_master = account_flag(is_master)
        table = entity_names.owner(self.conn, 'accounts').table
        existing = self.conn.execute(f'SELECT brand_account_id FROM {table} WHERE label=?', (label,)).fetchone()
        if existing and (existing[0] or None)!=(brand_account_id or None):
            raise ValueError('An existing account cannot be retargeted; add the other account separately')
        self.conn.execute(
            _UPSERT.format(table=table),
            (label, credential_ref, brand_account_id, is_master))
        self.conn.commit()
        row = self.conn.execute(f"SELECT id FROM {table} WHERE label=?", (label,)).fetchone()
        return row["id"]

    @synchronized
    def replace_accounts(self, configs) -> dict[str, int]:
        """Reconcile the saved config, retaining removed rows for their history and cached data."""
        with self.reconciled(configs) as ids:
            return ids

    @contextmanager
    def reconciled(self, configs):
        """Keep reconciliation unpublished until the caller has prepared its config/runtime."""
        configs = tuple(configs)
        with self._lock:
            table = entity_names.owner(self.conn, 'accounts').table
            self.conn.execute("SAVEPOINT setup_identities")
            try:
                existing = list(self.conn.execute(f"SELECT * FROM {table}"))
                selected, used = _select(configs, existing)
                self.conn.execute(f"UPDATE {table} SET active=0, is_master=0")
                labels = {cfg.label for cfg,_ in selected}
                for old in existing:
                    if old['id'] not in used and old['label'] in labels:
                        # Reusing a label for a new remote account must not reuse its local ID.
                        retired = f"@inactive:{old['id']}:{old['label']}"
                        while self.conn.execute(f'SELECT 1 FROM {table} WHERE label=?', (retired,)).fetchone():
                            retired += ':'
                        self.conn.execute(f'UPDATE {table} SET label=? WHERE id=?', (retired,old['id']))
                # Temporary labels allow two existing accounts to exchange display labels.
                for _, iid in selected:
                    if iid is not None:
                        self.conn.execute(f"UPDATE {table} SET label=? WHERE id=?", (f"@setup:{iid}:{uuid4().hex}", iid))
                ids = {}
                for cfg, iid in selected:
                    if iid is None:
                        iid = self.conn.execute(f"INSERT INTO {table}(label,credential_ref,brand_account_id,is_master,active) "
                            "VALUES (?,?,?,?,1)", (cfg.label,cfg.credential_ref,cfg.brand_account_id,int(cfg.is_master))).lastrowid
                    else:
                        self.conn.execute(f"INSERT INTO {table}(id,label,credential_ref,brand_account_id,is_master,active) "
                            "VALUES (?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET label=excluded.label, "
                            "credential_ref=excluded.credential_ref,brand_account_id=excluded.brand_account_id, "
                            "is_master=excluded.is_master,active=1", (iid,cfg.label,cfg.credential_ref,cfg.brand_account_id,int(cfg.is_master)))
                    ids[cfg.label] = iid
                yield ids
                # Temporary zero-master states are valid inside this transaction.
                # A configured result must contain exactly the selected accounts
                # and one active master, even when the caller performed writes.
                rows = self.conn.execute(f'SELECT id,is_master,active FROM {table}').fetchall()
                for row in rows:
                    account_flag(row['is_master'])
                    account_flag(row['active'], 'active')
                active = [row for row in rows if row['active'] == 1]
                if ({row['id'] for row in active} != set(ids.values())
                        or sum(row['is_master'] for row in active) != 1):
                    raise ValueError('Configured accounts must have exactly one active master')
                self.conn.execute("RELEASE setup_identities")
            except BaseException:
                self.conn.execute("ROLLBACK TO setup_identities")
                self.conn.execute("RELEASE setup_identities")
                raise

    def get_accounts(self) -> list[Account]:
        # Compaction cannot change accounts. This finite snapshot also lets
        # incoming playback frames retain exact attribution while all main
        # database handles are briefly closed for replacement.
        cached = getattr(self._store, '_maintenance_accounts', None)
        if getattr(self._store, 'activity_paused', False) and cached is not None:
            return list(cached)
        return self._get_accounts()

    @lookup_read
    def _get_accounts(self):
        table = entity_names.owner(self.conn, 'accounts').table
        rows = self.conn.execute(f"SELECT * FROM {table} WHERE active=1").fetchall()
        return [Account(r["id"], r["label"], r["credential_ref"], r["brand_account_id"],
                         bool(r["is_master"]), r["last_auth_ok"]) for r in rows]

    def get_master_account(self):
        try:
            return next(i for i in self.get_accounts() if i.is_master)
        except StopIteration:
            raise ValueError("No master identity configured")
