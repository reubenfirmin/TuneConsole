"""AccountRepo: the YouTube Music accounts the app manages."""
from yt_playlist.repos.base import Repo, synchronized, lookup_read
from yt_playlist.repos.models import Account
from yt_playlist.core import account_auth_state, entity_names
from yt_playlist.core.db_writes import atomic_write
from uuid import uuid4


_UPSERT = (
    "INSERT INTO {table}(label, credential_ref, brand_account_id, is_master, active) "
    "VALUES (?,?,?,?,1) ON CONFLICT(label) DO UPDATE SET "
    "credential_ref=excluded.credential_ref, brand_account_id=excluded.brand_account_id, "
    "is_master=excluded.is_master, active=1 "
    "WHERE COALESCE(brand_account_id,'')=COALESCE(excluded.brand_account_id,'') RETURNING id"
)


class AccountRepo(Repo):
    @lookup_read
    def has_auth_state(self):
        return account_auth_state.available(self.conn)

    @lookup_read
    def auth_expirations(self):
        return account_auth_state.read(self.conn)

    def flag_auth_expiration(self, account_id, label):
        if self._store.activity_paused:
            return None
        with atomic_write(self._store):
            if self._store.activity_paused:
                return None
            account_auth_state.flag(self.conn, account_id, label)
            return account_auth_state.read(self.conn)

    def clear_auth_expirations(self, account_id=None):
        if self._store.activity_paused:
            return None
        with atomic_write(self._store):
            if self._store.activity_paused:
                return None
            account_auth_state.clear(self.conn, account_id)
            return account_auth_state.read(self.conn)

    @lookup_read
    def account_rows(self, *, active=False):
        table = entity_names.owner(self.conn, 'accounts').table
        where = ' WHERE active=1' if active else ''
        return [dict(row) for row in self.conn.execute(
            f'SELECT id,label,credential_ref,brand_account_id,is_master,active FROM {table}{where} ORDER BY id')]

    @lookup_read
    def account_by_label(self, label):
        table = entity_names.owner(self.conn, 'accounts').table
        row = self.conn.execute(f'SELECT * FROM {table} WHERE label=?', (label,)).fetchone()
        return dict(row) if row else None

    @synchronized
    def upsert_account(self, label, credential_ref, brand_account_id, is_master):
        table = entity_names.owner(self.conn, 'accounts').table
        with self.conn.atomic(join=True):
            row = self.conn.execute(_UPSERT.format(table=table),
                                    (label, credential_ref, brand_account_id, is_master)).fetchone()
            if row is None:
                raise ValueError('An existing account cannot be retargeted; add the other account separately')
            return row[0]

    @synchronized
    def replace_accounts(self, rows, *, expected) -> dict[str, int]:
        """Commit selected bindings, retaining inactive IDs and their history."""
        with self.conn.atomic(join=True):
            table = entity_names.owner(self.conn, 'accounts').table
            self.conn.execute(f'UPDATE {table} SET active=active WHERE 0')
            if self.account_rows() != expected:
                raise ValueError('Account bindings changed; reload their configuration')
            existing = self.conn.execute(f'SELECT id,label FROM {table}').fetchall()
            used = {row[4] for row in rows if row[4] is not None}
            labels = {row[0] for row in rows}
            self.conn.execute(f'UPDATE {table} SET active=0, is_master=0')
            for old in existing:
                if old['id'] not in used and old['label'] in labels:
                    retired = f"@inactive:{old['id']}:{old['label']}"
                    while self.conn.execute(f'SELECT 1 FROM {table} WHERE label=?', (retired,)).fetchone():
                        retired += ':'
                    self.conn.execute(f'UPDATE {table} SET label=? WHERE id=?', (retired, old['id']))
            # Avoid unique-label conflicts while existing IDs exchange labels.
            for iid in used:
                self.conn.execute(f'UPDATE {table} SET label=? WHERE id=?', (f'@setup:{iid}:{uuid4().hex}', iid))
            ids = {}
            for label, credential, brand, master, iid in rows:
                if iid is None:
                    iid = self.conn.execute(f'INSERT INTO {table}(label,credential_ref,brand_account_id,is_master,active) '
                        'VALUES (?,?,?,?,1)', (label, credential, brand, master)).lastrowid
                else:
                    self.conn.execute(f'UPDATE {table} SET label=?,credential_ref=?,brand_account_id=?,is_master=?,active=1 '
                                      'WHERE id=?', (label, credential, brand, master, iid))
                ids[label] = iid
            return ids

    @lookup_read
    def get_accounts(self) -> list[Account]:
        table = entity_names.owner(self.conn, 'accounts').table
        rows = self.conn.execute(f"SELECT * FROM {table} WHERE active=1").fetchall()
        return [Account(r["id"], r["label"], r["credential_ref"], r["brand_account_id"],
                         bool(r["is_master"]), r["last_auth_ok"]) for r in rows]
