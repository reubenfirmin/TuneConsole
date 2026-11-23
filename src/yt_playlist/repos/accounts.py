"""AccountRepo: the YouTube Music accounts the app manages."""
from yt_playlist.repos.base import Repo, synchronized, lookup_read
from yt_playlist.repos.models import Account
from yt_playlist.core.db_writes import atomic_write
from uuid import uuid4


_UPSERT = (
    "INSERT INTO accounts(label, brand_account_id, is_master, active) "
    "VALUES (?,?,?,1) ON CONFLICT(label) DO UPDATE SET "
    "is_master=excluded.is_master, active=1 "
    "WHERE COALESCE(brand_account_id,'')=COALESCE(excluded.brand_account_id,'') RETURNING id"
)


class AccountRepo(Repo):
    def _auth_expirations(self):
        return dict(self.conn.execute('SELECT id,label FROM accounts WHERE auth_expired=1'))

    @lookup_read
    def auth_expirations(self):
        return self._auth_expirations()

    def flag_auth_expiration(self, account_id, label):
        if self._store.activity_paused:
            return None
        with atomic_write(self._store):
            if self._store.activity_paused:
                return None
            if type(account_id) is not int or account_id < 1 or not isinstance(label, str):
                raise ValueError('Expired authentication requires an exact account and label')
            if not self.conn.execute('UPDATE accounts SET auth_expired=1 WHERE id=?', (account_id,)).rowcount:
                raise ValueError('Expired authentication requires a recorded account')
            return self._auth_expirations()

    def clear_auth_expirations(self, account_id=None):
        if self._store.activity_paused:
            return None
        with atomic_write(self._store):
            if self._store.activity_paused:
                return None
            if account_id is None:
                self.conn.execute('UPDATE accounts SET auth_expired=0 WHERE auth_expired=1')
            else:
                self.conn.execute('UPDATE accounts SET auth_expired=0 WHERE id=? AND auth_expired=1', (account_id,))
            return self._auth_expirations()

    @lookup_read
    def bindings_saved(self):
        return self.conn.execute('SELECT 1 FROM accounts WHERE bindings_saved=1 LIMIT 1').fetchone() is not None

    @lookup_read
    def account_rows(self):
        return [dict(row) for row in self.conn.execute(
            'SELECT id,label,brand_account_id,is_master,active FROM accounts ORDER BY id')]

    @lookup_read
    def account_by_label(self, label):
        row = self.conn.execute('SELECT * FROM accounts WHERE label=?', (label,)).fetchone()
        return dict(row) if row else None

    @synchronized
    def upsert_account(self, label, brand_account_id, is_master):
        with self.conn.atomic(join=True):
            row = self.conn.execute(_UPSERT,
                                    (label, brand_account_id, is_master)).fetchone()
            if row is None:
                raise ValueError('An existing account cannot be retargeted; add the other account separately')
            return row[0]

    @synchronized
    def replace_accounts(self, rows, *, expected, importing=False) -> dict[str, int]:
        """Commit selected bindings, retaining inactive IDs and their history."""
        with self.conn.atomic(join=True):
            self.conn.execute('UPDATE accounts SET active=active WHERE 0')
            if importing and self.bindings_saved():
                raise ValueError('Account bindings have already been imported')
            if self.account_rows() != expected:
                raise ValueError('Account bindings changed; reload their configuration')
            existing = self.conn.execute('SELECT id,label FROM accounts').fetchall()
            existing_ids = {row['id'] for row in existing}
            used = {iid for iid, *_ in rows if iid is not None}
            labels = {label for _, label, *_ in rows}
            self.conn.execute('UPDATE accounts SET active=0, is_master=0')
            for old in existing:
                if old['id'] not in used and old['label'] in labels:
                    retired = f"@inactive:{old['id']}:{old['label']}"
                    while self.conn.execute('SELECT 1 FROM accounts WHERE label=?', (retired,)).fetchone():
                        retired += ':'
                    self.conn.execute('UPDATE accounts SET label=? WHERE id=?', (retired, old['id']))
            # Avoid unique-label conflicts while existing IDs exchange labels.
            for iid in used & existing_ids:
                self.conn.execute('UPDATE accounts SET label=? WHERE id=?', (f'@setup:{iid}:{uuid4().hex}', iid))
            ids = {}
            # Reserve explicit IDs before allocating IDs for newly added accounts.
            for iid, label, brand, master in sorted(rows, key=lambda row: row[0] is None):
                if iid in existing_ids:
                    self.conn.execute('UPDATE accounts SET label=?,brand_account_id=?,is_master=?,active=1,'
                                      'bindings_saved=max(bindings_saved,?) WHERE id=?',
                                      (label, brand, master, master, iid))
                else:
                    iid = self.conn.execute(
                        'INSERT INTO accounts(id,label,brand_account_id,is_master,active,bindings_saved) '
                        'VALUES (?,?,?,?,1,?)', (iid, label, brand, master, master)).lastrowid
                ids[label] = iid
            return ids

    @lookup_read
    def get_accounts(self) -> list[Account]:
        rows = self.conn.execute('SELECT id,label,brand_account_id,is_master FROM accounts WHERE active=1 ORDER BY id')
        return [Account(r['id'], r['label'], r['brand_account_id'], bool(r['is_master'])) for r in rows]
